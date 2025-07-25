# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import json
import re
import sys
from datetime import timedelta
from logging import getLogger
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.auth.models import User as DjangoUser
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.functions import Length
from django.db.models.signals import post_delete, post_save
from django.dispatch.dispatcher import receiver
from django.utils import timezone
from enumfields import Enum, EnumField
from notifications.signals import notify

from webcompat.models import Report, Signature
from webcompat.symptoms import URLSymptom, ValueMatcher

if getattr(settings, "USE_CELERY", None):
    from .tasks import triage_new_report

if sys.version_info[:2] < (3, 12):
    from server.utils import batched
else:
    from itertools import batched

LOG = getLogger("reportmanager")


# these enable `{field}__length` filtering in Django
models.CharField.register_lookup(Length)
models.TextField.register_lookup(Length)


class App(models.Model):
    channel = models.CharField(max_length=63, null=True)
    name = models.CharField(max_length=63)
    version = models.CharField(max_length=127)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("channel", "name", "version"), name="unique_app"
            ),
        )


class BreakageCategory(models.Model):
    value = models.CharField(max_length=63, unique=True)


class Bucket(models.Model):
    bug = models.ForeignKey("Bug", null=True, on_delete=models.deletion.CASCADE)
    color = models.ForeignKey(
        "BucketColor", null=True, on_delete=models.deletion.CASCADE
    )
    description = models.TextField()
    # store the domain outside the signature only if the signature includes
    # a non-regex domain symptom and no other symptoms (for quick exclusion)
    domain = models.CharField(max_length=255, null=True)
    # bucket can be hidden from view until given date, without being logged
    hide_until = models.DateTimeField(blank=True, null=True)
    # higher priority = earlier match
    priority = models.IntegerField(
        default=0, validators=(MinValueValidator(-2), MaxValueValidator(2))
    )
    # Raw signature JSON (see webcompat.models.Signature)
    signature = models.TextField()
    reassign_in_progress = models.BooleanField(default=False)

    class Meta:
        constraints = (
            models.CheckConstraint(
                check=models.Q(priority__gte=-2) & models.Q(priority__lte=2),
                name="priority_range",
            ),
        )

    @property
    def watchers(self):
        ids = User.objects.filter(
            bucketwatch__bucket=self, bucket_hit=True
        ).values_list("user_id", flat=True)
        return DjangoUser.objects.filter(id__in=ids).distinct()

    def get_signature(self):
        return Signature(self.signature)

    def save(self, *args, **kwargs):
        modified = set()

        # Sanitize signature line endings so we end up with the same hash
        new_signature = json.dumps(json.loads(self.signature), sort_keys=True)
        if new_signature != self.signature:
            modified.add("signature")
            self.signature = new_signature

            # if signature changes, update domain field
            sig_obj = self.get_signature()
            original_domain = self.domain
            if (
                len(sig_obj.symptoms) == 1
                and isinstance(symptom := sig_obj.symptoms[0], URLSymptom)
                and symptom.part == "hostname"
                and isinstance(symptom.matcher, ValueMatcher)
            ):
                self.domain = symptom.matcher.value
            else:
                self.domain = None
            if original_domain != self.domain:
                modified.add("domain")

        # required in Django 4.2+
        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            kwargs["update_fields"] = modified.union(kwargs["update_fields"])

        super().save(*args, **kwargs)

    def reassign(self, submit_save, limit=None, offset=None):
        """Assign all issues that match our signature to this bucket from
        lower-priority buckets. Furthermore, remove all non-matching issues
        from our bucket (which will be re-triaged into default buckets).

        We only actually save if "submit_save" is set.
        For previewing, we just count how many issues would be assigned and removed.
        """
        from .serializers import ReportEntryVueSerializer

        in_list, out_list = [], []
        in_list_count, out_list_count = 0, 0

        signature = self.get_signature()
        entries = ReportEntry.objects.filter(
            models.Q(bucket__priority__lt=self.priority) | models.Q(bucket=self)
        ).select_related(
            # these are used by get_report
            "app",
            "breakage_category",
            "os",
        )

        if not submit_save:
            entries = entries.order_by("-id")  # used by the preview list

        # implement limit/offset pagination of reassignment to support
        # batched requests from frontend
        if limit is not None:
            assert limit > 0
            assert offset is None or offset >= 0
        else:
            assert offset is None

        entry_ids = entries.values_list("id", flat=True)
        if offset:
            entry_ids = entry_ids[offset:]

        next_offset = None
        if limit:
            remainder = entry_ids.count()
            if remainder > limit:
                next_offset = (offset or 0) + limit
            entry_ids = entry_ids[:limit]

        # If we are saving, we only care about the id of each entry
        # Otherwise, we save the entire object. Limit to the first 100 entries to avoid
        # OOM.
        MATCH_BATCH_SIZE = 100
        for entry_ids_batch in batched(entry_ids, MATCH_BATCH_SIZE):
            for entry in entries.filter(id__in=entry_ids_batch):
                match = signature.matches(entry.get_report())
                if match and entry.bucket != self:
                    if submit_save:
                        in_list.append(entry.pk)
                    elif len(in_list) < MATCH_BATCH_SIZE:
                        in_list.append(ReportEntryVueSerializer(entry).data)
                    in_list_count += 1
                elif not match and entry.bucket == self:
                    if submit_save:
                        out_list.append(entry.pk)
                    elif len(out_list) < MATCH_BATCH_SIZE:
                        out_list.append(ReportEntryVueSerializer(entry).data)
                    out_list_count += 1

        if submit_save:
            UPDATE_BATCH_SIZE = 500
            for entry_ids_batch in batched(in_list, UPDATE_BATCH_SIZE):
                for report in ReportEntry.objects.filter(pk__in=entry_ids_batch).values(
                    "bucket_id",
                    "reported_at",
                ):
                    if report["bucket_id"] != self.id:
                        if report["bucket_id"] is not None:
                            BucketHit.decrement_count(
                                report["bucket_id"],
                                report["reported_at"],
                            )
                        BucketHit.increment_count(self.id, report["reported_at"])
                ReportEntry.objects.filter(pk__in=entry_ids_batch).update(bucket=self)
            for entry_ids_batch in batched(out_list, UPDATE_BATCH_SIZE):
                for report in ReportEntry.objects.filter(pk__in=entry_ids_batch).values(
                    "bucket_id", "reported_at"
                ):
                    if report["bucket_id"] is not None:
                        BucketHit.decrement_count(
                            report["bucket_id"], report["reported_at"]
                        )
                ReportEntry.objects.filter(pk__in=entry_ids_batch).update(bucket=None)

        return in_list, out_list, in_list_count, out_list_count, next_offset

    def optimize_signature(self, unbucketed_entries):
        buckets = Bucket.objects.all()

        signature = self.get_signature()

        entries = unbucketed_entries

        optimized_signature = None
        matching_entries = []

        # Avoid hitting the database multiple times when looking for the first
        # entry of a bucket. Keeping these in memory is less expensive.
        first_entry_per_bucket_cache = {}

        for entry in entries:
            entry.reportinfo = entry.get_report()

            # For optimization, disregard any issues that directly match since those
            # could be incoming new issues and we don't want these to block the
            # optimization.
            if signature.matches(entry.reportinfo):
                continue

            optimized_signature = signature.fit(entry.reportinfo)
            if optimized_signature:
                # We now try to determine how this signature will behave in other
                # buckets. If the signature matches lots of other buckets as well, it is
                # likely too broad and we should not consider it (or later rate it worse
                # than others).
                matches_in_other_buckets = False
                non_matches_in_other_buckets = 0  # noqa
                other_matching_bucket_ids = []  # noqa
                for other_bucket in buckets:
                    if other_bucket.pk == self.pk:
                        continue

                    if (
                        self.bug
                        and other_bucket.bug
                        and self.bug.pk == other_bucket.bug.pk
                    ):
                        # Allow matches in other buckets if they are both linked to the
                        # same bug
                        continue

                    if other_bucket.pk not in first_entry_per_bucket_cache:
                        c = ReportEntry.objects.filter(
                            bucket=other_bucket
                        ).select_related("app", "breakage_category", "os")
                        c = c.first()
                        first_entry_per_bucket_cache[other_bucket.pk] = c
                        if c:
                            first_entry_per_bucket_cache[other_bucket.pk] = (
                                c.get_report()
                            )

                    first_entry_report = first_entry_per_bucket_cache[other_bucket.pk]
                    if first_entry_report and optimized_signature.matches(
                        first_entry_report
                    ):
                        matches_in_other_buckets = True
                        break

                if matches_in_other_buckets:
                    # Reset, we don't actually have an optimized signature if it's
                    # matching some other bucket as well.
                    optimized_signature = None
                else:
                    for other_entry in entries:
                        other_entry.reportinfo = other_entry.get_report()
                        if optimized_signature.matches(other_entry.reportinfo):
                            matching_entries.append(other_entry)

                    # Fallback for when the optimization algorithm failed for some
                    # reason
                    if not matching_entries:
                        optimized_signature = None

                    break

        return (optimized_signature, matching_entries)


class BucketColor(models.Model):
    name = models.CharField(max_length=255, unique=True)
    value = models.IntegerField(
        unique=True, validators=(MinValueValidator(0), MaxValueValidator(0xFFFFFF))
    )

    class Meta:
        constraints = (
            models.CheckConstraint(
                check=models.Q(value__gte=0) & models.Q(value__lte=0xFFFFFF),
                name="value_range",
            ),
        )


class BugProvider(models.Model):
    classname = models.CharField(max_length=255, blank=False)
    hostname = models.CharField(max_length=255, blank=False)

    # This is used to annotate bugs with the URL linking to them
    url_template = models.CharField(max_length=1023, blank=False)

    def get_instance(self):
        # Dynamically instantiate the provider as requested
        provider_module = __import__(
            f"reportmanager.Bugtracker.{self.classname}", fromlist=[self.classname]
        )
        provider_cls = getattr(provider_module, self.classname)
        return provider_cls(self.pk, self.hostname)

    def __str__(self):
        return self.hostname


class Bug(models.Model):
    external_id = models.CharField(max_length=255, blank=True)
    external_type = models.ForeignKey(BugProvider, on_delete=models.deletion.CASCADE)
    closed = models.DateTimeField(blank=True, null=True)


def buckethit_default_range_begin():
    return timezone.now().replace(microsecond=0, second=0, minute=0)


class BucketHit(models.Model):
    bucket = models.ForeignKey(Bucket, on_delete=models.deletion.CASCADE)
    begin = models.DateTimeField(default=buckethit_default_range_begin)
    count = models.IntegerField(default=0)

    @classmethod
    @transaction.atomic
    def decrement_count(cls, bucket_id, begin):
        begin = begin.replace(microsecond=0, second=0, minute=0)
        counter = (
            cls.objects.select_for_update()
            .filter(
                bucket_id=bucket_id,
                begin=begin,
            )
            .first()
        )
        if counter is not None and counter.count > 0:
            counter.count -= 1
            counter.save()

    @classmethod
    @transaction.atomic
    def increment_count(cls, bucket_id, begin):
        begin = begin.replace(microsecond=0, second=0, minute=0)
        counter, _ = cls.objects.select_for_update().get_or_create(
            bucket_id=bucket_id, begin=begin
        )
        counter.count += 1
        counter.save()

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["bucket", "begin"],
                name="unique_buckethits",
            ),
        )


class BucketWatch(models.Model):
    user = models.ForeignKey("User", on_delete=models.deletion.CASCADE)
    bucket = models.ForeignKey(Bucket, on_delete=models.deletion.CASCADE)
    # This is the primary key of last crash marked viewed by the user
    # Store as an integer to prevent problems if the particular crash
    # is deleted later. We only care about its place in the ordering.
    last_report = models.IntegerField(default=0)


class OS(models.Model):
    name = models.CharField(max_length=63, unique=True)


class ReportHit(models.Model):
    last_update = models.DateTimeField(default=timezone.now)
    count = models.BigIntegerField(default=0)

    @staticmethod
    def get_period(time):
        """Return the current time if we are on the hour, or the next
        time the hour will change.
        """
        if not time.minute and not time.second and not time.microsecond:
            # we're on the hour. this is the time
            return time
        # next hour
        return time + timedelta(
            hours=1,
            minutes=-time.minute,
            seconds=-time.second,
            microseconds=-time.microsecond,
        )

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["last_update"],
                name="unique_reporthits",
            ),
        )


class ReportEntryManager(models.Manager):
    @transaction.atomic
    def create_from_report(self, report, bucket_id=None):
        app = App.objects.get_or_create(
            channel=report.app_channel,
            name=report.app_name,
            version=report.app_version,
        )[0]
        if report.breakage_category is not None:
            breakage = BreakageCategory.objects.get_or_create(
                value=report.breakage_category,
            )[0]
        else:
            breakage = None
        os = OS.objects.get_or_create(name=report.os)[0]
        return self.create(
            app=app,
            breakage_category=breakage,
            url=report.url.geturl(),
            uuid=report.uuid,
            reported_at=report.reported_at,
            os=os,
            details=report.details,
            comments=report.comments,
            comments_translated=report.comments_translated,
            comments_original_language=report.comments_original_language,
            ml_valid_probability=report.ml_valid_probability,
            bucket_id=bucket_id,
        )


class ReportEntry(models.Model):
    app = models.ForeignKey(App, on_delete=models.deletion.CASCADE)
    breakage_category = models.ForeignKey(
        BreakageCategory, null=True, on_delete=models.deletion.CASCADE
    )
    bucket = models.ForeignKey(Bucket, null=True, on_delete=models.deletion.CASCADE)
    comments = models.TextField()
    comments_translated = models.TextField(null=True)
    comments_original_language = models.TextField(null=True)
    details = models.JSONField()
    os = models.ForeignKey(OS, on_delete=models.deletion.CASCADE)
    reported_at = models.DateTimeField()
    url = models.URLField(max_length=8192)
    uuid = models.UUIDField(unique=True)
    ml_valid_probability = models.FloatField(null=True)

    objects = ReportEntryManager()

    def __init__(self, *args, **kwargs):
        self._cached_report = None
        self._original_bucket = None
        super().__init__(*args, **kwargs)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._original_bucket = instance.bucket_id
        return instance

    def save(self, *args, **kwargs):
        modified = set()

        if self.pk is None and not getattr(settings, "DB_ISUTF8MB4", False):
            # Replace 4-byte UTF-8 characters with U+FFFD if our database
            # doesn't support them. By default, MySQL utf-8 does not support these.
            utf8_4byte_re = re.compile("[^\u0000-\ud7ff\ue000-\uffff]", re.UNICODE)

            def sanitize_utf8(s):
                if not isinstance(s, str):
                    s = str(s, "utf-8")

                return utf8_4byte_re.sub("\ufffd", s)

            comments = sanitize_utf8(self.comments)
            if self.comments != comments:
                self.comments = comments
                modified.add("comments")

        # required in Django 4.2+
        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            kwargs["update_fields"] = modified.union(kwargs["update_fields"])

        super().save(*args, **kwargs)

    def get_report(self):
        if self._cached_report is None:
            self._cached_report = Report(
                app_channel=self.app.channel,
                app_name=self.app.name,
                app_version=self.app.version,
                comments=self.comments,
                comments_translated=self.comments_translated,
                comments_original_language=self.comments_original_language,
                details=self.details,
                os=self.os.name,
                reported_at=self.reported_at,
                uuid=self.uuid,
                url=urlsplit(self.url),
                breakage_category=(
                    self.breakage_category.value
                    if self.breakage_category is not None
                    else None
                ),
                ml_valid_probability=self.ml_valid_probability,
            )
        return self._cached_report

    def reparse_report(self):
        # Purges cached report information and then forces a reparsing
        # of the raw report information. Based on the new report information,
        # the depending fields are also repopulated.
        #
        # This method should only be called if either the raw report information
        # has changed or the implementation parsing it was updated.
        self._cached_report = None
        report = self.get_report()

        # If the entry has a bucket, check if it still fits into
        # this bucket, otherwise remove it.
        if self.bucket is not None:
            sig = self.bucket.get_signature()
            report = self.get_report()
            if not sig.matches(report):
                self.bucket = None

        return self.save()


@receiver(post_delete, sender=ReportEntry)
def ReportEntry_delete(sender, instance, **kwargs):
    if instance.bucket_id is not None:
        BucketHit.decrement_count(instance.bucket_id, instance.reported_at)


@receiver(post_save, sender=ReportEntry)
def ReportEntry_save(sender, instance, created, **kwargs):
    triage = created

    if instance.bucket_id != instance._original_bucket:
        if instance._original_bucket is not None:
            # remove BucketHit for old bucket
            BucketHit.decrement_count(instance._original_bucket, instance.reported_at)

        if instance.bucket is not None:
            # add BucketHit for new bucket
            BucketHit.increment_count(instance.bucket_id, instance.reported_at)
        else:
            triage = True

        if instance.bucket is not None:
            notify.send(
                instance.bucket,
                recipient=instance.bucket.watchers,
                actor=instance.bucket,
                verb="bucket_hit",
                target=instance,
                level="info",
                description=(
                    f"The bucket {instance.bucket_id} received a new report entry "
                    f"{instance.pk}"
                ),
            )

    if getattr(settings, "USE_CELERY", None) and triage:
        triage_new_report.apply_async((instance.pk,), countdown=0.1)


class BugzillaTemplateMode(Enum):
    Bug = "bug"
    Comment = "comment"


class BugzillaTemplate(models.Model):
    mode = EnumField(BugzillaTemplateMode, max_length=30)
    name = models.TextField()
    product = models.TextField()
    component = models.TextField()
    summary = models.TextField(blank=True)
    version = models.TextField()
    description = models.TextField(blank=True)
    whiteboard = models.TextField(blank=True)
    keywords = models.TextField(blank=True)
    op_sys = models.TextField(blank=True)
    platform = models.TextField(blank=True)
    priority = models.TextField(blank=True)
    severity = models.TextField(blank=True)
    alias = models.TextField(blank=True)
    cc = models.TextField(blank=True)
    assigned_to = models.TextField(blank=True)
    qa_contact = models.TextField(blank=True)
    target_milestone = models.TextField(blank=True)
    attrs = models.TextField(blank=True)
    security = models.BooleanField(blank=False, default=False)
    security_group = models.TextField(blank=True)
    comment = models.TextField(blank=True)
    blocks = models.TextField(blank=True)
    dependson = models.TextField(blank=True)

    def __str__(self):
        return self.name


class User(models.Model):
    class Meta:
        permissions = (
            (
                "reportmanager_visible",
                "Can see ReportManager app (required for reportmanager_* perms)",
            ),
            ("reportmanager_write", "Write access to ReportManager"),
            ("reportmanager_read", "Read access to ReportManager"),
        )

    user = models.OneToOneField(DjangoUser, on_delete=models.deletion.CASCADE)
    # Explicitly do not store this as a ForeignKey to e.g. BugzillaTemplate
    # because the bug provider has to decide how to interpret this ID.
    default_template_id = models.IntegerField(default=0)
    default_provider_id = models.IntegerField(default=1)
    buckets_watching = models.ManyToManyField(Bucket, through="BucketWatch")

    # Notifications
    bucket_hit = models.BooleanField(blank=False, default=False)
    inaccessible_bug = models.BooleanField(blank=False, default=False)


@receiver(post_save, sender=DjangoUser)
def add_default_perms(sender, instance, created, **kwargs):
    if created:
        for perm in getattr(settings, "DEFAULT_PERMISSIONS", []):
            model, perm = perm.split(":", 1)
            module, model = model.rsplit(".", 1)
            module = __import__(
                module, globals(), locals(), [model], 0
            )  # from module import model
            content_type = ContentType.objects.get_for_model(getattr(module, model))
            perm = Permission.objects.get(content_type=content_type, codename=perm)
            instance.user_permissions.add(perm)
            LOG.info("user %s added permission %s:%s", instance.username, model, perm)
