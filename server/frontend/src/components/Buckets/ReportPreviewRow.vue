<template>
  <tr v-on:click="report.view_url">
    <td class="wrap-normal">{{ report.reported_at | shorterDate }}</td>
    <td class="url-col">
      <a :href="report.url" target="_blank" rel="noreferrer">
        {{ report.url }}
      </a>
    </td>
    <td class="wrap-normal comments-col">
      <div>
        {{ maybeTranslatedComments(report) }}
      </div>
    </td>
    <td>
      <img
        v-if="report.os === 'Linux'"
        width="16px"
        height="16px"
        alt="Linux"
        :src="staticLogo('linux')"
      />
      <img
        v-else-if="report.os === 'Mac'"
        width="16px"
        height="16px"
        alt="macOS"
        :src="staticLogo('macosx')"
      />
      <img
        v-else-if="report.os === 'Windows'"
        width="16px"
        height="16px"
        alt="Windows"
        :src="staticLogo('windows')"
      />
      <img
        v-else-if="report.os === 'Android'"
        width="16px"
        height="16px"
        alt="Android"
        :src="staticLogo('android')"
      />
      <span v-else>{{ report.os }}</span>
    </td>
    <td>{{ report.app_name }}</td>
    <td>{{ report.app_channel }}</td>
    <td>{{ report.app_version }}</td>
    <td>{{ report.breakage_category }}</td>
    <td>
      {{
        report.details.boolean
          .broken_site_report_tab_info_antitracking_has_tracking_content_blocked
      }}
    </td>
    <td>
      {{
        report.details.string
          .broken_site_report_tab_info_antitracking_block_list
      }}
    </td>
    <td>
      {{
        report.details.boolean
          .broken_site_report_tab_info_antitracking_is_private_browsing
      }}
    </td>
  </tr>
</template>

<script>
import { shorterDate } from "../../helpers";

export default {
  props: {
    report: {
      type: Object,
      required: true,
    },
  },
  filters: {
    shorterDate: shorterDate,
  },
  methods: {
    staticLogo(name) {
      return window.location.origin + "/static/img/os/" + name + ".png";
    },
    maybeTranslatedComments(report) {
      if (
        report.comments_original_language &&
        report.comments_original_language !== "en"
      ) {
        // The translation pipeline does escape HTML, and the "easiest" way to
        // un-escape that is by just assignign it to an element and letting
        // the browser do the magic.. :/
        let el = document.createElement("span");
        el.innerHTML = `[${report.comments_original_language}] ${report.comments_translated}`;
        return el.innerText;
      }

      return report.comments;
    },
  },
};
</script>

<style scoped>
.comments-col {
  overflow-wrap: anywhere;

  div {
    max-height: 150px;
    overflow: scroll;
  }
}

.url-col a {
  display: block;
  max-width: 300px;
  overflow: scroll;
  text-overflow: ellipsis;

  /*
   * This is to ensure the overlay scrollbar isn't overlaying the text. Ideally,
   * this link would be `height: 100%`, but bug 1598458 is a thing.
   */
  padding-bottom: 1.2em;
}
</style>
