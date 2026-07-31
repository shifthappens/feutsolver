/* Minimal Streamlit v1 component protocol bridge.
 * Kept local so the board does not depend on a CDN's ESM dependency graph.
 */
(function () {
  "use strict";

  const Streamlit = {
    API_VERSION: 1,
    RENDER_EVENT: "streamlit:render",
    events: new EventTarget(),
    _ready: false,

    setComponentReady() {
      if (!this._ready) {
        window.addEventListener("message", (event) => {
          const data = event.data;
          if (!data || data.type !== this.RENDER_EVENT) return;
          this.events.dispatchEvent(
            new CustomEvent(this.RENDER_EVENT, {
              detail: {
                args: data.args || {},
                disabled: Boolean(data.disabled),
                theme: data.theme || null,
              },
            }),
          );
        });
        this._ready = true;
      }

      window.parent.postMessage(
        {
          isStreamlitMessage: true,
          type: "streamlit:componentReady",
          apiVersion: this.API_VERSION,
        },
        "*",
      );
    },

    setFrameHeight(height) {
      const measured = height || document.body.scrollHeight;
      window.parent.postMessage(
        {
          isStreamlitMessage: true,
          type: "streamlit:setFrameHeight",
          height: measured,
        },
        "*",
      );
    },

    setComponentValue(value) {
      window.parent.postMessage(
        {
          isStreamlitMessage: true,
          type: "streamlit:setComponentValue",
          dataType: "json",
          value,
        },
        "*",
      );
    },
  };

  window.Streamlit = Streamlit;
})();
