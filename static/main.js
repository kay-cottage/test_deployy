(function(){
  const $ = (sel) => document.querySelector(sel);
  const urlInput = $('#urlInput');
  const fileInput = $('#fileInput');
  const extractBtn = $('#extractBtn');
  const clearBtn = $('#clearBtn');
  const resultsEl = $('#results');
  const statusEl = $('#status');
  const statsEl = $('#stats');
  const btnTxt = $('#downloadTxt');
  const btnJson = $('#downloadJson');

  function status(t, isErr=false){
    statusEl.textContent = t || '';
    statusEl.classList.toggle('err', !!isErr);
  }
  function stats(t){ statsEl.textContent = t || ''; }

  clearBtn.addEventListener('click', () => {
    urlInput.value = '';
    fileInput.value = '';
    resultsEl.innerHTML = '';
    status('');
    stats('');
    toggleDownloads(false);
  });

  extractBtn.addEventListener('click', async () => {
    resultsEl.innerHTML = '';
    status('处理中…');
    stats('');
    toggleDownloads(false);
    try {
      const resp = await doExtract();
      render(resp.messages || []);
      status(`完成。共提取 ${resp.count||0} 条消息。`);
      stats(`${new Date().toLocaleString()} · 提取 ${resp.count||0} 条`);
      toggleDownloads((resp.count||0) > 0, resp.messages||[]);
    } catch (err) {
      console.error(err);
      status(err.message || String(err), true);
    }
  });

  async function doExtract(){
    if (fileInput.files && fileInput.files[0]) {
      const fd = new FormData();
      fd.append('html_file', fileInput.files[0]);
      const r = await fetch('/api/extract', { method: 'POST', body: fd });
      if (!r.ok) throw new Error(`后端返回错误：HTTP ${r.status}`);
      return await r.json();
    }
    const url = (urlInput.value || '').trim();
    if (!url) throw new Error('请输入分享链接或上传 HTML 文件');
    const r = await fetch('/api/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    if (!r.ok) throw new Error(`后端返回错误：HTTP ${r.status}`);
    return await r.json();
  }

  function toggleDownloads(enabled, data){
    btnTxt.disabled = !enabled;
    btnJson.disabled = !enabled;
    if(!enabled){ btnTxt.onclick = btnJson.onclick = null; return; }
    const txt = toTxt(data);
    const json = JSON.stringify(data.map((m,i)=>({idx:i+1, role:m.role, text:m.text})), null, 2);
    btnTxt.onclick = () => download('chat_messages.txt', txt, 'text/plain');
    btnJson.onclick = () => download('chat_messages.json', json, 'application/json');
  }

  function toTxt(messages){
    const parts = [];
    messages.forEach((m, i) => {
      parts.push(`--- ${i+1}. ${m.role.toUpperCase()} ---`);
      parts.push(m.text);
      parts.push('');
    });
    return parts.join('\n');
  }

  function download(filename, content, mime){
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; a.style.display = 'none';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=> URL.revokeObjectURL(url), 1000);
  }

  function render(messages){
    resultsEl.innerHTML = '';
    if (!messages.length){
      resultsEl.innerHTML = '<div class="small">未提取到消息。</div>';
      return;
    }
    messages.forEach((m, i) => {
      const box = document.createElement('div');
      box.className = 'msg';
      const meta = document.createElement('div');
      meta.className = 'meta';
      const role = document.createElement('span');
      role.className = 'role ' + (m.role === 'assistant' ? 'assistant' : 'user');
      role.textContent = m.role.toUpperCase();
      const idx = document.createElement('span'); idx.textContent = `#${i+1}`;
      meta.appendChild(role); meta.appendChild(idx);
      const txt = document.createElement('div');
      txt.className = 'txt';
      txt.textContent = m.text;
      box.appendChild(meta);
      box.appendChild(txt);
      resultsEl.appendChild(box);
    });
  }
})();
