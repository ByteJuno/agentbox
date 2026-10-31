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
