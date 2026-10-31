/* agent-hub：新建工作空间对话框里说明「名称」怎么对应到 lab 上的目录（原版前端不改，服务时注入）。 */
(function () {
  "use strict";
  function apply() {
    var name = document.getElementById("new-name");
    if (name) {
      name.removeAttribute("data-i18n-attrs");
      name.placeholder = "ai-work 下的文件夹名（已有就直接打开），或 ~/路径";
    }
    var desc = document.getElementById("new-desc");
    if (desc) {
      desc.removeAttribute("data-i18n-text");
      desc.textContent = "空间就是 lab 上的一个项目目录：已有的直接打开，没有的在 ~/ai-work 下新建。Claude 直接在 lab 上运行。";
    }
    // 这两项与 Docker 无关：把原控件移到可见的界面分区，保留上游的保存/校验监听。
    var section = document.getElementById("sec-interface");
    var parallel = document.getElementById("set-max-parallel");
    if (section && parallel) {
      var card = parallel.closest(".card");
      if (card && card.parentNode !== section) {
        var explanation = card.querySelector(".card-desc");
        if (explanation) {
          explanation.removeAttribute("data-i18n");
          explanation.textContent = "一个工作空间里最多几个对话线程可以同时运行。每个线程直接使用主机上的 Claude Code；超过上限的跟进消息等待空位。";
        }
        section.appendChild(card);
      }
    }
    var history = document.getElementById("set-history-timeout");
    if (section && history && !section.contains(history)) {
      var historyCard = document.createElement("div");
      historyCard.className = "card";
      historyCard.appendChild(history.closest(".field"));
      section.appendChild(historyCard);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", apply);
  else apply();
})();

/* 旧界面登录过的浏览器（有 hub 的 cookie）：自动换成 agentbox 前端用的令牌，不用再到登录页输一遍。 */
(function () {
  "use strict";
  try {
    if (localStorage.getItem("agentbox_token")) return;
    if (sessionStorage.getItem("agent_hub_autologin")) return;   // 只试一次，失败就留在登录页
    sessionStorage.setItem("agent_hub_autologin", "1");
  } catch (e) { return; }
  fetch("/hub/token", { credentials: "same-origin" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (d && d.token) { localStorage.setItem("agentbox_token", d.token); location.reload(); }
    })
    .catch(function () {});
})();
