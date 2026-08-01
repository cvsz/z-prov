const form = document.querySelector('#chat-form');
const output = document.querySelector('#output');
const statusEl = document.querySelector('#chat-status');
const send = document.querySelector('#send');

function eventText(protocol, eventName, data) {
  if (eventName === 'error') return '\n[' + ((data && data.message) || 'Gateway error') + ']';
  if (protocol === 'anthropic' && eventName === 'content_block_delta') return (data.delta && data.delta.text) || '';
  if (protocol === 'chat') return ((data.choices || [])[0] || {}).delta?.content || '';
  if (protocol === 'responses' && eventName === 'response.output_text.delta') return data.delta || '';
  return '';
}

async function consume(response, protocol) {
  if (!response.ok) throw new Error('Request failed (HTTP ' + response.status + ')');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const result = await reader.read();
    if (result.done) break;
    buffer += decoder.decode(result.value, {stream: true});
    const events = buffer.split('\n\n');
    buffer = events.pop() || '';
    for (const event of events) {
      const dataLine = event.split('\n').find((line) => line.startsWith('data:'));
      if (!dataLine || dataLine.slice(5).trim() === '[DONE]') continue;
      const eventName = (event.match(/^event:\s*(.*)$/m) || [])[1];
      try { output.textContent += eventText(protocol, eventName, JSON.parse(dataLine.slice(5))); }
      catch (_) { /* provider deltas that are not JSON are not rendered */ }
    }
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const protocol = document.querySelector('#protocol').value;
  const model = document.querySelector('#model').value.trim();
  const prompt = document.querySelector('#prompt').value.trim();
  if (!model || !prompt) return;
  send.disabled = true;
  statusEl.textContent = 'Streaming…';
  output.textContent = '';
  const messages = [{role: 'user', content: prompt}];
  const payload = protocol === 'anthropic' ? {model, max_tokens: 4096, messages} : protocol === 'chat' ? {model, messages} : {model, input: prompt};
  try {
    const response = await fetch('/api/chat/' + protocol, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(payload)});
    await consume(response, protocol);
    statusEl.textContent = 'Complete';
  } catch (error) {
    output.textContent += '\n\n' + error.message;
    statusEl.textContent = 'Failed';
  } finally { send.disabled = false; }
});
