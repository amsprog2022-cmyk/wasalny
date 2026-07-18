/* Wassalny inbox — vanilla JS + Socket.IO */
(() => {
  let activeConvId = null;
  const socket = io("/inbox");

  const chatEmpty = document.getElementById("chat-empty");
  const chat = document.getElementById("chat");
  const chatPeerName = document.getElementById("chat-peer-name");
  const chatPeerWa = document.getElementById("chat-peer-wa");
  const chatMessages = document.getElementById("chat-messages");
  const composerFree = document.getElementById("composer-free");
  const composerLocked = document.getElementById("composer-locked");
  const composerInput = document.getElementById("composer-input");
  const composerSend = document.getElementById("composer-send");
  const templateSelect = document.getElementById("template-select");
  const templateSend = document.getElementById("template-send");
  const windowStatus = document.getElementById("chat-window-status");

  // Load a conversation into the chat panel
  async function openConversation(convId, meta) {
    activeConvId = convId;
    chatEmpty.classList.add("hidden");
    chat.classList.remove("hidden");
    chat.classList.add("flex");
    chatPeerName.textContent = meta.peerName;
    chatPeerWa.textContent = meta.peerWa;

    // Show/hide composer based on 24h window
    if (meta.withinWindow) {
      composerFree.classList.remove("hidden");
      composerLocked.classList.add("hidden");
      composerLocked.classList.remove("flex");
      windowStatus.textContent = "● Free window open";
      windowStatus.className = "text-xs text-emerald-600";
    } else {
      composerFree.classList.add("hidden");
      composerLocked.classList.remove("hidden");
      windowStatus.textContent = "🔒 Window closed — template required";
      windowStatus.className = "text-xs text-slate-500";
    }

    // Clear unread badge
    const item = document.querySelector(`.conv-item[data-conv-id="${convId}"]`);
    if (item) {
      const badge = item.querySelector(".unread-badge");
      if (badge) badge.remove();
    }

    // Mark as read on server
    fetch(`/inbox/${convId}/read`, { method: "POST" });

    // Load messages
    const res = await fetch(`/inbox/${convId}/messages`);
    const data = await res.json();
    chatMessages.innerHTML = "";
    for (const msg of data.messages) appendMessage(msg);
    scrollToBottom();
  }

  function appendMessage(msg) {
    const el = document.createElement("div");
    const isOut = msg.direction === "outbound";
    el.className = `flex ${isOut ? "justify-end" : "justify-start"}`;
    const bubble = document.createElement("div");
    bubble.className = `max-w-[70%] rounded-lg px-3 py-2 text-sm shadow-sm ${
      isOut ? "bg-emerald-500 text-white" : "bg-white text-slate-800"
    }`;
    bubble.textContent = msg.body || `[${msg.msg_type}]`;
    const meta = document.createElement("div");
    meta.className = `text-[10px] mt-1 ${isOut ? "text-emerald-100" : "text-slate-400"}`;
    const time = msg.created_at ? new Date(msg.created_at).toLocaleTimeString() : "";
    meta.textContent = `${time} · ${msg.status}`;
    bubble.appendChild(meta);
    el.appendChild(bubble);
    chatMessages.appendChild(el);
  }

  function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  // Click on a conversation
  document.getElementById("conv-list").addEventListener("click", (e) => {
    const item = e.target.closest(".conv-item");
    if (!item) return;
    document
      .querySelectorAll(".conv-item")
      .forEach((el) => el.classList.remove("bg-emerald-50"));
    item.classList.add("bg-emerald-50");
    openConversation(parseInt(item.dataset.convId), {
      peerName: item.dataset.peerName,
      peerWa: item.dataset.peerWa,
      withinWindow: item.dataset.withinWindow === "true",
    });
  });

  // Send free-form text
  async function sendText() {
    const body = composerInput.value.trim();
    if (!body || !activeConvId) return;
    composerInput.value = "";
    const res = await fetch(`/inbox/${activeConvId}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert("Failed to send: " + (err.error || res.statusText));
      composerInput.value = body;
    }
  }
  composerSend.addEventListener("click", sendText);
  composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendText();
    }
  });

  // Send template
  templateSend.addEventListener("click", async () => {
    const opt = templateSelect.options[templateSelect.selectedIndex];
    if (!opt.value || !activeConvId) return;
    const varCount = parseInt(opt.dataset.vars || "0");
    const variables = [];
    for (let i = 0; i < varCount; i++) {
      const v = prompt(`Enter value for variable {{${i + 1}}}:`);
      if (v === null) return;
      variables.push(v);
    }
    const res = await fetch(`/inbox/${activeConvId}/send-template`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        template_name: opt.value,
        language: opt.dataset.lang,
        variables,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert("Failed: " + (err.error || res.statusText));
    }
  });

  // Real-time updates
  socket.on("new_message", (payload) => {
    const { conversation, message } = payload;

    // Update conversation list
    let item = document.querySelector(
      `.conv-item[data-conv-id="${conversation.id}"]`
    );
    if (item) {
      // Move to top, update preview
      const list = document.getElementById("conv-list");
      list.prepend(item);
      const preview = item.querySelector(".text-xs.text-slate-600");
      if (preview) preview.textContent = conversation.last_message_preview || "—";
      item.dataset.withinWindow = conversation.within_free_window ? "true" : "false";
    }

    // Append to open chat
    if (activeConvId === conversation.id) {
      appendMessage(message);
      scrollToBottom();
      if (message.direction === "inbound") {
        fetch(`/inbox/${conversation.id}/read`, { method: "POST" });
      }
    } else if (message.direction === "inbound" && item) {
      // Bump unread badge
      let badge = item.querySelector(".unread-badge");
      if (!badge) {
        badge = document.createElement("span");
        badge.className =
          "unread-badge bg-emerald-500 text-white text-xs rounded-full px-2 py-0.5";
        item.querySelector(".flex.justify-between").appendChild(badge);
        badge.textContent = "1";
      } else {
        badge.textContent = String(parseInt(badge.textContent || "0") + 1);
      }
    }
  });
})();
