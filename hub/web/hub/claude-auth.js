/* agent-hub：设置 → 账号池 → 「lab 主机 Claude 登录」卡片（原版前端不改，服务时注入）。
   只做三件事：看 claude auth status、拿一次官方授权链接、把授权码交给服务器；链接和授权码都只放在内存里。 */
(function () {
  "use strict";

  var card, badge, detail, msg, flowBox, link, codeInput, startBtn, cancelBtn, submitBtn, refreshBtn, copyBtn, expiry;
  var timer = null;
  var currentFlow = null;
  var handledTerminal = "";

  function token() {
    try { return localStorage.getItem("agentbox_token") || ""; } catch (e) { return ""; }
  }

  function setToken(value) {
    try { localStorage.setItem("agentbox_token", value); } catch (e) {}
  }

  function node(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function request(path, options) {
    options = options || {};
    var headers = { "Accept": "application/json" };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    var secret = token();
    if (secret) headers["Authorization"] = "Bearer " + secret;
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      credentials: "same-origin",
      cache: "no-store",
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    }).then(function (response) {
      if (response.status === 401 && !options.retried) {
        // 和 hub.js 一样：旧界面留下的 cookie 换一次令牌；换不到就明确让人重新登录。
        return fetch("/hub/token", { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; })
          .then(function (data) {
            if (!data || !data.token) throw new Error("登录已失效，请刷新页面重新登录");
            setToken(data.token);
            options.retried = true;
            return request(path, options);
          });
      }
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok || !data || data.ok === false) {
          throw new Error((data && data.error) || ("请求失败（HTTP " + response.status + "）"));
        }
        return data;
      });
    });
  }

  function fmtTime(value) {
    if (!value) return "";
    var date = typeof value === "number" ? new Date(value) : new Date(String(value));
    if (isNaN(date.getTime())) return String(value).slice(0, 40);
    return date.toLocaleString();
  }

  function renderStatus(data) {
    var known = data.available !== false;
    var logged = data.loggedIn === true;
    badge.textContent = !known ? "状态未知" : (logged ? "已登录" : "未登录");
    badge.className = "ca-badge " + (!known ? "ca-unknown" : (logged ? "ca-on" : "ca-off"));
    var bits = [];
    if (!known) {
      bits.push(data.error || "读取登录状态失败");
    } else if (logged) {
      if (data.account) bits.push("账号 " + data.account);
      if (data.loginExpiresAt) bits.push("登录有效期至 " + fmtTime(data.loginExpiresAt));
      if (data.authMethod && data.authMethod !== "none") bits.push("方式 " + data.authMethod);
    } else {
      bits.push("未登录时 Claude 会话无法在这台机器上运行");
    }
    if (startBtn) startBtn.textContent = logged ? "重新登录（获取官方链接）" : "获取官方登录链接";
    if (data.checkedAt) bits.push("检查于 " + new Date(data.checkedAt).toLocaleTimeString());
    detail.textContent = bits.join(" · ");
  }

  function stopPoll() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function startPoll() {
    if (timer) return;
    timer = setInterval(function () {
      request("/hub/claude-auth/flow").then(function (data) {
        applyFlow(data.flow);
        if (!data.flow) { stopPoll(); loadStatus(); }
      }).catch(function (error) {
        stopPoll();
        msg.textContent = error.message;
      });
    }, 2000);
  }

  function loadStatus() {
    return request("/hub/claude-auth/status").then(function (data) {
      renderStatus(data);
      if (data.flow) applyFlow(data.flow);
      return data;
    }).catch(function (error) {
      badge.textContent = "读取失败";
      badge.className = "ca-badge ca-unknown";
      detail.textContent = error.message;
    });
  }

  function setBusy(busy) {
    refreshBtn.disabled = busy;
    startBtn.disabled = busy;
    cancelBtn.disabled = busy;
    submitBtn.disabled = busy || (currentFlow && currentFlow.phase === "verifying");
  }

  function applyFlow(flow) {
    currentFlow = flow || null;
    if (!flow) {
      flowBox.hidden = true;
      startBtn.hidden = false;
      cancelBtn.hidden = true;
      stopPoll();
      setBusy(false);
      return;
    }
    if (flow.id === handledTerminal) return;
    var active = flow.phase === "starting" || flow.phase === "waiting" || flow.phase === "verifying";
    if (active) {
      flowBox.hidden = false;
      startBtn.hidden = true;
      cancelBtn.hidden = false;
      startPoll();
      var mine = link.getAttribute("data-flow") === flow.id && link.href;
      if (!mine) {
        // 链接只在发起它的那次响应里出现，刷新页面后不重放；取消后可以重新拿一条。
        link.removeAttribute("href");
        link.textContent = "（登录链接只在获取它的那次页面里显示；如果已经关掉，请取消后重新获取）";
        copyBtn.hidden = true;
      } else {
        copyBtn.hidden = false;
      }
      expiry.textContent = "这条链接只对这次登录有效，最晚 " + new Date(flow.expiresAt).toLocaleTimeString() + " 失效。";
      if (flow.phase === "verifying") {
        msg.textContent = "已提交授权码，正在等 Anthropic 确认…";
        submitBtn.disabled = true;
      } else {
        submitBtn.disabled = false;
        if (flow.error) msg.textContent = flow.error;
      }
      setBusy(false);
      return;
    }
    // 结束态：只处理一次，之后重新读 auth status
    handledTerminal = flow.id;
    stopPoll();
    flowBox.hidden = true;
    cancelBtn.hidden = true;
    startBtn.hidden = false;
    codeInput.value = "";   // 不管结果如何都不留授权码
    submitBtn.disabled = false;
    if (flow.phase === "succeeded") msg.textContent = "登录成功：claude auth status 已显示已登录。";
    else if (flow.phase === "cancelled") msg.textContent = "已取消这次登录。";
    else msg.textContent = flow.error || "这次登录没有完成。";
    setBusy(false);
    loadStatus();
  }

  function build() {
    var section = document.getElementById("sec-accounts");
    if (!section || document.getElementById("claude-auth-card")) return;

    card = node("div", "card ca-card");
    card.id = "claude-auth-card";
    card.appendChild(node("h3", "", "lab 主机 Claude 登录"));
    card.appendChild(node("p", "card-desc",
      "查看 Claude Code 在这台机器上的登录状态。需要登录时只生成 Anthropic 官方的授权链接，由你本人在官方页面完成；hub 不接触密码，不保存授权码。已经登录时也可以重新走一遍，成功后才替换现有登录。"));

    var state = node("div", "ca-state");
    badge = node("span", "ca-badge ca-unknown", "读取中…");
    detail = node("span", "ca-detail");
    state.appendChild(badge);
    state.appendChild(detail);
    card.appendChild(state);

    var actions = node("div", "ca-actions");
    refreshBtn = node("button", "btn btn-sm btn-ghost", "刷新状态");
    refreshBtn.type = "button";
    startBtn = node("button", "btn btn-sm btn-primary", "获取官方登录链接");
    startBtn.type = "button";
    cancelBtn = node("button", "btn btn-sm btn-danger", "取消这次登录");
    cancelBtn.type = "button";
    cancelBtn.hidden = true;
    actions.appendChild(refreshBtn);
    actions.appendChild(startBtn);
    actions.appendChild(cancelBtn);
    card.appendChild(actions);

    flowBox = node("div", "ca-flow");
    flowBox.hidden = true;
    flowBox.appendChild(node("p", "note",
      "1. 点下面的官方链接，用你的 Claude 账号登录并授权。2. 把页面给出的整行授权码（通常是 code#state）粘贴到输入框提交。"));
    link = node("a", "ca-url");
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    flowBox.appendChild(link);

    var copyRow = node("div", "ca-copy");
    copyBtn = node("button", "btn btn-sm btn-ghost", "复制链接");
    copyBtn.type = "button";
    copyRow.appendChild(copyBtn);
    copyRow.appendChild(node("span", "note", "链接含一次性授权参数，别转发给别人。"));
    flowBox.appendChild(copyRow);

    var field = node("label", "field ca-code-field");
    field.appendChild(node("span", "", "授权码（只用于这次登录，不保存）"));
    codeInput = node("input", "mono");
    codeInput.type = "password";
    codeInput.autocomplete = "off";
    codeInput.spellcheck = false;
    codeInput.autocapitalize = "off";
    codeInput.placeholder = "code#state";
    field.appendChild(codeInput);
    flowBox.appendChild(field);

    var submitRow = node("div", "ca-actions");
    submitBtn = node("button", "btn btn-sm btn-primary", "提交授权码");
    submitBtn.type = "button";
    submitRow.appendChild(submitBtn);
    flowBox.appendChild(submitRow);

    expiry = node("p", "note ca-expiry");
    flowBox.appendChild(expiry);
    card.appendChild(flowBox);

    msg = node("p", "note ca-msg");
    msg.setAttribute("role", "status");
    msg.setAttribute("aria-live", "polite");
    card.appendChild(msg);

    section.appendChild(card);

    refreshBtn.addEventListener("click", function () { msg.textContent = ""; loadStatus(); });
    startBtn.addEventListener("click", function () {
      setBusy(true);
      msg.textContent = "正在向 claude 申请官方登录链接…";
      request("/hub/claude-auth/start", { method: "POST", body: {} }).then(function (data) {
        handledTerminal = "";
        link.href = data.authorizeUrl;
        link.textContent = data.authorizeUrl;
        link.setAttribute("data-flow", data.flow.id);
        copyBtn.hidden = false;
        codeInput.value = "";
        applyFlow(data.flow);
        msg.textContent = "打开上面的官方链接完成授权，再把授权码粘贴到下面。";
      }).catch(function (error) {
        msg.textContent = error.message;
        setBusy(false);
      });
    });
    cancelBtn.addEventListener("click", function () {
      setBusy(true);
      request("/hub/claude-auth/cancel", { method: "POST", body: {} }).then(function (data) {
        codeInput.value = "";
        applyFlow(data.flow);
        if (!data.cancelled) msg.textContent = "当前没有进行中的登录。";
      }).catch(function (error) {
        msg.textContent = error.message;
        setBusy(false);
      });
    });
    submitBtn.addEventListener("click", function () {
      var value = codeInput.value.trim();
      codeInput.value = "";   // 提交后立刻清掉，不进页面历史也不留在内存里
      if (!value) { msg.textContent = "请先粘贴授权码。"; return; }
      setBusy(true);
      msg.textContent = "正在提交授权码…";
      request("/hub/claude-auth/code", { method: "POST", body: { code: value } }).then(function (data) {
        value = "";
        applyFlow(data.flow);
        if (data.flow && data.flow.phase === "waiting") msg.textContent = data.flow.error || "授权码没有被接受，请重新复制。";
        else if (data.flow && data.flow.phase === "verifying") msg.textContent = "已提交，等待 Anthropic 确认…";
      }).catch(function (error) {
        value = "";
        msg.textContent = error.message;
        setBusy(false);
      });
    });
    copyBtn.addEventListener("click", function () {
      var text = link.href || "";
      if (!text) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          msg.textContent = "链接已复制；只发给自己的浏览器用。";
        }).catch(function () {
          msg.textContent = "复制失败，请长按上面的链接手动复制。";
        });
      } else {
        msg.textContent = "这个浏览器不支持自动复制，请长按链接手动复制。";
      }
    });
    loadStatus();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
