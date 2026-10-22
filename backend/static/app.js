const $ = (selector) => document.querySelector(selector);
const input = $('#prompt-input');
const messages = $('#messages');
const tracePanel = $('#trace-panel');
const timeline = $('#timeline');
const welcome = $('#welcome');
let busy = false;
let latestPrompt = '';
let runStartedAt = 0;
let currentRun = 0;
let traceEvents = [];

function setBackend(ok, label, sub = '') {
  for (const dot of [$('#top-dot'), $('#side-dot')]) {
    dot.classList.toggle('ok', ok === true);
    dot.classList.toggle('error', ok === false);
  }
  $('#top-status').textContent = label;
  $('#side-status').textContent = label;
  $('#side-model').textContent = sub || 'Local agent workspace';
}

async function checkHealth() {
  try {
    const response = await fetch('/health');
    if (!response.ok) throw new Error('Backend unavailable');
    const data = await response.json();
    setBackend(true, data.api_key_configured ? 'Backend ready' : 'Backend online', data.model_name || 'Backboard connection');
    if (!data.api_key_configured) $('#side-model').textContent = 'Add BACKBOARD_API_KEY to chat';
  } catch {
    setBackend(false, 'Backend offline', 'Start the API server to chat');
  }
}

function resizeInput() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  $('#char-count').textContent = `${input.value.length} / 2000`;
  $('#send-button').disabled = busy || !input.value.trim();
}

function scrollToBottom() {
  const area = $('#chat-scroll');
  area.scrollTop = area.scrollHeight;
}

function addMessage(role, text, options = {}) {
  welcome.classList.add('hidden');
  const row = document.createElement('div');
  row.className = `message ${role}${options.error ? ' message-error' : ''}`;
  if (role === 'user') {
    const body = document.createElement('div');
    body.className = 'message-body';
    const name = document.createElement('div');
    name.className = 'message-name';
    name.textContent = 'You';
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.textContent = text;
    body.append(name, bubble);
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar user-avatar';
    avatar.textContent = 'Y';
    row.append(body, avatar);
  } else {
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar agent-avatar';
    avatar.textContent = '✳';
    const body = document.createElement('div');
    body.className = 'message-body';
    const name = document.createElement('div');
    name.className = 'message-name';
    name.textContent = options.error ? 'Request error' : 'SprintMasters agent';
    const bubble = document.createElement('div');
    bubble.className = options.error ? 'message-bubble message-error' : 'message-bubble';
    bubble.textContent = text;
    body.append(name, bubble);
    if (options.meta) {
      const meta = document.createElement('div');
      meta.className = 'message-meta';
      meta.textContent = options.meta;
      body.append(meta);
    }
    row.append(avatar, body);
  }
  messages.append(row);
  scrollToBottom();
  return row;
}

function addTyping() {
  const row = document.createElement('div');
  row.className = 'message';
  row.id = 'typing-indicator';
  const avatar = document.createElement('div');
  avatar.className = 'message-avatar agent-avatar';
  avatar.textContent = '✳';
  const body = document.createElement('div');
  body.className = 'message-body';
  const name = document.createElement('div');
  name.className = 'message-name';
  name.textContent = 'SprintMasters agent';
  const dots = document.createElement('div');
  dots.className = 'typing';
  dots.innerHTML = '<i></i><i></i><i></i>';
  body.append(name, dots);
  row.append(avatar, body);
  messages.append(row);
  scrollToBottom();
}

function removeTyping() { $('#typing-indicator')?.remove(); }

function showTrace() {
  $('#trace-empty').classList.add('hidden');
  $('#trace-content').classList.remove('hidden');
}

function setTab(name) {
  document.querySelectorAll('.trace-tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('hidden', panel.id !== `${name}-panel`));
}

function renderEvents() {
  timeline.replaceChildren();
  for (const event of traceEvents) {
    const item = document.createElement('div');
    item.className = `timeline-step ${event.state}`;
    const icon = document.createElement('span');
    icon.className = 'timeline-icon';
    icon.textContent = event.state === 'done' ? '✓' : event.state === 'current' ? '·' : '○';
    const copy = document.createElement('div');
    copy.className = 'timeline-copy';
    const title = document.createElement('strong');
    title.textContent = event.title;
    const description = document.createElement('p');
    description.textContent = event.description;
    copy.append(title, description);
    if (event.time) {
      const time = document.createElement('div');
      time.className = 'timeline-time';
      time.textContent = event.time;
      copy.append(time);
    }
    item.append(icon, copy);
    timeline.append(item);
  }
  $('#activity-count').textContent = String(traceEvents.length);
  $('#trace-count').textContent = String(traceEvents.length);
}

function elapsed() { return `${((performance.now() - runStartedAt) / 1000).toFixed(1)}s`; }

function addEvent(title, description, state = 'done', time = '') {
  traceEvents.push({ title, description, state, time });
  renderEvents();
}

function setRunState(state) {
  const badge = $('#run-state');
  badge.className = `run-state ${state}`;
  badge.textContent = state.toUpperCase();
}

function renderDetails(data, duration) {
  const values = [
    ['Request status', data.status || 'COMPLETED'],
    ['Duration', `${duration.toFixed(2)} s`],
    ['Provider', data.model_provider || 'Not reported'],
    ['Model', data.model_name || 'Default model'],
    ['Input tokens', data.usage?.input_tokens ?? 'Not reported'],
    ['Output tokens', data.usage?.output_tokens ?? 'Not reported'],
    ['Total tokens', data.usage?.total_tokens ?? 'Not reported'],
    ['Cost', data.usage?.cost_usd == null ? 'Not reported' : `$${data.usage.cost_usd}`],
    ['Thread ID', data.thread_id || 'Not reported'],
    ['Assistant ID', data.assistant_id || 'Not reported'],
  ];
  const list = $('#detail-list');
  list.replaceChildren();
  for (const [label, value] of values) {
    const row = document.createElement('div');
    row.className = 'detail-row';
    const left = document.createElement('span');
    left.textContent = label;
    const right = document.createElement('strong');
    right.textContent = String(value);
    row.append(left, right);
    list.append(row);
  }
}

async function sendPrompt(value = input.value) {
  const prompt = value.trim();
  if (!prompt || busy) return;
  busy = true;
  currentRun += 1;
  runStartedAt = performance.now();
  latestPrompt = prompt;
  traceEvents = [];
  showTrace();
  $('#run-name').textContent = prompt.length > 35 ? `${prompt.slice(0, 35)}…` : prompt;
  $('#prompt-preview').textContent = prompt;
  setRunState('running');
  setTab('activity');
  addEvent('Prompt received', 'Your message is ready to send to the configured Backboard agent.', 'done', '0.0s');
  addMessage('user', prompt);
  $('#history-title').textContent = prompt.length > 26 ? `${prompt.slice(0, 26)}…` : prompt;
  input.value = '';
  resizeInput();
  const startedAt = performance.now();
  const run = currentRun;
  addEvent('Request sent', 'POST /api/test · streaming and web search are off.', 'current', '0.0s');
  addTyping();
  $('#send-button').disabled = true;
  try {
    const response = await fetch('/api/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof data.detail === 'object' ? data.detail.message : data.detail;
      const error = new Error(detail || `Request failed (${response.status})`);
      error.code = typeof data.detail === 'object' ? data.detail.code : '';
      error.status = response.status;
      throw error;
    }
    if (run !== currentRun) return;
    const duration = (performance.now() - startedAt) / 1000;
    traceEvents[traceEvents.length - 1].state = 'done';
    traceEvents[traceEvents.length - 1].time = `${duration.toFixed(1)}s`;
    addEvent('Response received', 'Backboard returned a completed response.', 'done', `${duration.toFixed(1)}s`);
    addEvent('Run complete', `Completed in ${Number(data.elapsed_ms || duration * 1000).toFixed(0)} ms.`, 'done', `${elapsed()} total`);
    renderEvents();
    renderDetails(data, Number(data.elapsed_ms || duration * 1000) / 1000);
    setRunState('complete');
    addMessage('assistant', data.reply, { meta: [data.model_provider, data.model_name].filter(Boolean).join(' · ') || `Completed in ${duration.toFixed(1)}s` });
  } catch (error) {
    if (run !== currentRun) return;
    const duration = (performance.now() - startedAt) / 1000;
    traceEvents[traceEvents.length - 1].state = 'error';
    traceEvents[traceEvents.length - 1].description = error.message;
    traceEvents[traceEvents.length - 1].time = `${duration.toFixed(1)}s`;
    addEvent('Run failed', error.code ? `${error.code} · HTTP ${error.status}` : 'The request could not be completed.', 'error', `${duration.toFixed(1)}s`);
    renderEvents();
    setRunState('error');
    addMessage('assistant', error.message, { error: true });
  } finally {
    removeTyping();
    if (run === currentRun) busy = false;
    resizeInput();
  }
}

$('#send-button').addEventListener('click', () => sendPrompt());
input.addEventListener('input', resizeInput);
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendPrompt();
  }
});
document.querySelectorAll('.suggestion').forEach((button) => button.addEventListener('click', () => sendPrompt(button.dataset.prompt)));
$('#new-chat').addEventListener('click', () => {
  currentRun += 1;
  busy = false;
  latestPrompt = '';
  traceEvents = [];
  messages.replaceChildren();
  welcome.classList.remove('hidden');
  $('#trace-content').classList.add('hidden');
  $('#trace-empty').classList.remove('hidden');
  $('#history-title').textContent = 'New conversation';
  $('#run-state').textContent = 'IDLE';
  $('#run-state').className = 'run-state';
  $('#trace-count').textContent = '0';
  input.focus();
});
document.querySelectorAll('.trace-tab').forEach((tab) => tab.addEventListener('click', () => setTab(tab.dataset.tab)));
$('#trace-toggle').addEventListener('click', () => {
  if (window.matchMedia('(max-width: 820px)').matches) tracePanel.classList.toggle('mobile-open');
  else tracePanel.classList.toggle('collapsed');
});
$('#close-trace').addEventListener('click', () => tracePanel.classList.remove('mobile-open'));
$('#open-sidebar').addEventListener('click', () => {
  $('#sidebar').classList.add('open');
  $('#mobile-scrim').classList.add('visible');
});
$('#close-sidebar').addEventListener('click', () => {
  $('#sidebar').classList.remove('open');
  $('#mobile-scrim').classList.remove('visible');
});
$('#mobile-scrim').addEventListener('click', () => {
  $('#sidebar').classList.remove('open');
  $('#mobile-scrim').classList.remove('visible');
});
document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    $('#new-chat').click();
  }
  if (event.key === 'Escape') tracePanel.classList.remove('mobile-open');
});

checkHealth();
setInterval(checkHealth, 15000);
resizeInput();
