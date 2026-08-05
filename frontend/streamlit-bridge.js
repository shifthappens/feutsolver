/* Minimal Streamlit v1 component protocol bridge.
 * Kept local so the board does not depend on a CDN's ESM dependency graph.
 */
(function () {
  "use strict";

  const MAX_MESSAGE_BYTES = 256 * 1024;

  function parentOrigin() {
    try {
      const referrer = document.referrer;
      if (referrer) {
        const origin = new URL(referrer).origin;
        if (origin && origin !== "null") return origin;
      }
      return window.location.origin && window.location.origin !== "null" ? window.location.origin : null;
    } catch (_error) {
      return null;
    }
  }

  function withinLimit(value) {
    try { return JSON.stringify(value).length <= MAX_MESSAGE_BYTES; } catch (_error) { return false; }
  }

  function postToParent(message) {
    const origin = parentOrigin();
    if (!origin || !withinLimit(message) || !window.parent || window.parent === window) return false;
    window.parent.postMessage(message, origin);
    return true;
  }

  const Streamlit = {
    API_VERSION: 1,
    RENDER_EVENT: "streamlit:render",
    events: new EventTarget(),
    _ready: false,

    setComponentReady() {
      if (!this._ready) {
        window.addEventListener("message", (event) => {
          const data = event.data;
          const origin = parentOrigin();
          if (!origin || event.source !== window.parent || event.origin !== origin) return;
          if (!data || typeof data !== "object" || data.type !== this.RENDER_EVENT || !withinLimit(data)) return;
          if (data.args !== undefined && (!data.args || typeof data.args !== "object" || Array.isArray(data.args))) return;
          this.events.dispatchEvent(
            new CustomEvent(this.RENDER_EVENT, {
              detail: {
                args: data.args && typeof data.args === "object" ? data.args : {},
                disabled: Boolean(data.disabled),
                theme: data.theme || null,
              },
            }),
          );
        });
        this._ready = true;
      }

      postToParent(
        {
          isStreamlitMessage: true,
          type: "streamlit:componentReady",
          apiVersion: this.API_VERSION,
        },
      );
    },

    setFrameHeight(height) {
      const measured = height || document.body.scrollHeight;
      postToParent(
        {
          isStreamlitMessage: true,
          type: "streamlit:setFrameHeight",
          height: measured,
        },
      );
    },

    setComponentValue(value) {
      postToParent(
        {
          isStreamlitMessage: true,
          type: "streamlit:setComponentValue",
          dataType: "json",
          value,
        },
      );
    },
  };

  window.Streamlit = Streamlit;
})();
