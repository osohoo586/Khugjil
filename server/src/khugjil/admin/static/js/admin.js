// Admin panel behaviour. No inline scripts anywhere (strict CSP); text is only ever set with
// textContent, never innerHTML.
(function () {
  "use strict";

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');

  // HTMX requests carry the CSRF token as a header.
  document.addEventListener("htmx:configRequest", function (event) {
    if (csrfMeta) event.detail.headers["X-CSRF-Token"] = csrfMeta.content;
  });

  // HTMX responses targeted at the shared dialog open it as a modal.
  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (target && target.id === "htmx-dialog" && !target.open) target.showModal();
    startCountdowns(target);
  });

  // Every destructive action asks first, naming the object (data-confirm on the form).
  var dialog = document.getElementById("confirm-dialog");
  var pendingForm = null;
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm || form.dataset.confirmed) return;
    event.preventDefault();
    if (!dialog) { if (window.confirm(form.dataset.confirm)) { form.dataset.confirmed = "1"; form.submit(); } return; }
    pendingForm = form;
    dialog.querySelector(".dialog-title").textContent = form.dataset.confirm;
    dialog.showModal();
  });
  if (dialog) {
    dialog.querySelector("[data-dialog-ok]").addEventListener("click", function () {
      dialog.close();
      if (pendingForm) { pendingForm.dataset.confirmed = "1"; pendingForm.submit(); }
    });
    dialog.querySelector("[data-dialog-cancel]").addEventListener("click", function () {
      pendingForm = null;
      dialog.close();
    });
  }

  document.addEventListener("click", function (event) {
    var el = event.target;
    if (!(el instanceof HTMLElement)) return;
    if (el.dataset.copy) {
      var source = document.getElementById(el.dataset.copy);
      if (source && navigator.clipboard) {
        navigator.clipboard.writeText(source.textContent.trim()).then(function () {
          var label = el.textContent;
          el.textContent = "Хуулсан";
          setTimeout(function () { el.textContent = label; }, 1500);
        });
      }
    }
    if (el.hasAttribute("data-dialog-close")) {
      var d = el.closest("dialog");
      if (d) { event.preventDefault(); d.close(); window.location.reload(); }
    }
  });

  // Pairing code countdown.
  function startCountdowns(root) {
    (root || document).querySelectorAll("[data-expires]").forEach(function (el) {
      var end = Date.parse(el.dataset.expires);
      function tick() {
        var left = Math.max(0, Math.round((end - Date.now()) / 1000));
        if (left === 0) { el.textContent = "Кодын хугацаа дууссан. Шинэ код үүсгэнэ үү."; return; }
        var m = Math.floor(left / 60), s = left % 60;
        el.textContent = m + ":" + (s < 10 ? "0" : "") + s + " минутын дотор, нэг удаа ашиглана.";
        setTimeout(tick, 1000);
      }
      tick();
    });
  }
  startCountdowns(document);

  // Invite links carry the token in the URL fragment; move it into the form and drop it from
  // the address bar so it never reaches a server log, history entry or Referer header.
  var tokenInput = document.getElementById("invite-token");
  if (tokenInput) {
    var match = /(?:^|[#&])t=([A-Za-z0-9_-]{20,128})/.exec(window.location.hash);
    history.replaceState(null, "", window.location.pathname);
    if (match) {
      tokenInput.value = match[1];
      document.getElementById("invite-start").submit();
    }
  }
})();
