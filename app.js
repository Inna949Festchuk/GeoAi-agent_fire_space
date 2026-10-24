// Небольшая интерактивность: сворачивание панелей и логика чата
document.querySelectorAll('.layer-collapse-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const panel = btn.closest('.panel');
    const collapsed = panel.classList.toggle('collapsed');
    btn.textContent = collapsed ? '▲' : '▼';
    btn.title = collapsed ? 'Развернуть' : 'Свернуть';
  });
});

const textarea = document.querySelector('.chat-input-row textarea');
const sendBtn = document.querySelector('.btn-send');
const messages = document.getElementById('chatMessages');

// Кнопка активна, только если есть текст (но всегда остаётся оранжевой)
textarea.addEventListener('input', () => {
  sendBtn.disabled = textarea.value.trim() === '';
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
});

textarea.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (!sendBtn.disabled) sendBtn.click();
  }
});

sendBtn.addEventListener('click', () => {
  const text = textarea.value.trim();
  if (!text) return;
  const msg = document.createElement('div');
  msg.className = 'chat-msg chat-msg-user';
  msg.textContent = text;
  messages.appendChild(msg);
  textarea.value = '';
  textarea.style.height = 'auto';
  sendBtn.disabled = true;
  messages.scrollTop = messages.scrollHeight;

  setTimeout(() => {
    const reply = document.createElement('div');
    reply.className = 'chat-msg chat-msg-bot';
    reply.textContent = 'Обрабатываю запрос о пожарах…';
    messages.appendChild(reply);
    messages.scrollTop = messages.scrollHeight;
  }, 600);
});
