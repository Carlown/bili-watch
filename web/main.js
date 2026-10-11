'use strict';
const $ = id => document.getElementById(id);
const labels = {pending:'待核实', reviewed:'已核实', dismissed:'无需处理', handled:'已人工处理'};
const reportLabels = {not_requested:'未进入队列', queued:'排队中', submitting:'提交中', submitted:'已提交', failed:'提交失败', needs_details:'待补充材料'};
let token = '', page = 1, result = null, editing = null, enabled = true, busy = false, lastEvent = null, settings = null;
let toastTimer;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4500); }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '本地请求失败');
  return data;
}
function element(tag, text, cls) { const el = document.createElement(tag); if(text !== undefined) el.textContent = text; if(cls) el.className = cls; return el; }
function link(text, url, cls) { const el = element('a',text,cls); el.href=url; el.target='_blank'; el.rel='noopener noreferrer'; return el; }
function date(text) { return text ? text.replace('T',' ').replace('+08:00','') : '—'; }
function render(data) {
  result = data; $('count').textContent = Object.values(data.counts).reduce((a,b)=>a+b,0);
  $('new-count').textContent = data.new_count; $('pending-count').textContent = data.counts.pending || 0;
  $('report-queue-count').textContent = (data.report_counts.queued || 0) + (data.report_counts.submitting || 0);
  $('report-submitted-count').textContent = data.report_counts.submitted || 0;
  $('report-details-count').textContent = data.report_counts.needs_details || 0;
  $('suspected-count').textContent = data.suspected_count || 0;
  $('rows').replaceChildren(); $('empty').hidden = data.videos.length > 0; $('table').hidden = data.videos.length === 0;
  if (!data.videos.length && (data.total || $('search').value || $('filter').value)) {
    $('empty').replaceChildren(element('strong','没有匹配的记录'),element('p','调整搜索内容或核实状态。'));
  }
  for (const video of data.videos) {
    const tr = element('tr');
    const name = element('td'); name.append(link(video.title, video.url));
    name.firstChild.appendChild(document.createTextNode(' ↗'));
    const meta = element('div',undefined,'meta'); if(!video.baseline) meta.append(element('span','新增','new-label'));
    meta.append(document.createTextNode(video.bvid + ' · ' + date(video.published_at))); name.append(meta);
    const source = element('td');
    if(video.source_url) source.append(link('打开原片线索 ↗',video.source_url,'source-link'));
    else source.append(link('到 YouTube 搜索 ↗','https://www.youtube.com/results?search_query='+encodeURIComponent(video.title),'source-link'));
    source.append(element('div',video.source_kind,'source-kind'));
    const coin = element('td'), marked = [1,2].includes(video.copyright);
    if (marked) {
      coin.append(element('span', video.coin_limit === 2 ? '可投 2 币 · 自制' : '仅可投 1 币 · 转载', 'badge coin-'+video.copyright));
      coin.append(element('div', video.copyright === 1 ? '疑似转载投自制' : '已标注转载', 'coin-verdict'+(video.copyright === 1 ? ' warn' : '')));
    } else if (video.coin_note) {
      coin.append(element('span','检测失败','badge coin-failed'), element('div', video.coin_note, 'coin-verdict'));
    } else {
      coin.append(element('span','未检测','badge'), element('div','尚未检测投币上限','coin-verdict'));
    }
    const status = element('td'); status.append(element('span', labels[video.status], 'badge '+video.status));
    const report = element('td'); report.append(element('span', reportLabels[video.report_status] || video.report_status, 'badge report-'+video.report_status));
    if (video.report_message) report.append(element('div', video.report_message, 'report-message'));
    const action = element('td'); const buttons=element('div',undefined,'row-actions');
    if (video.report_status === 'failed') {
      const retry=element('button','重试举报'); retry.addEventListener('click',()=>retryReport(video.bvid));
      buttons.append(retry);
    }
    const recoin=element('button','重测投币'); recoin.addEventListener('click',()=>probeCoin(video.bvid));
    const edit=element('button','核实 / 记笔记'); edit.addEventListener('click',()=>openEditor(video));
    buttons.append(recoin,edit,link('打开 B 站 ↗',video.url)); action.append(buttons);
    tr.append(name,source,coin,status,report,action); $('rows').append(tr);
  }
  $('page-info').textContent = `第 ${page} / ${Math.max(1,Math.ceil(data.total/50))} 页 · 当前筛选 ${data.total} 条`;
  $('prev').disabled = page <= 1; $('next-page').disabled = page*50 >= data.total;
  if(lastEvent !== null && data.event && data.event.id > lastEvent) {
    const message = `发现 ${data.event.count} 条新增投稿：${data.event.title}`;
    toast(message);
    if('Notification' in window && Notification.permission === 'granted') {
      const notice = new Notification('投稿观察：有新视频', {body:message,tag:'bili-watch-new'});
      notice.onclick=()=>{window.focus();notice.close();};
    }
  }
  lastEvent = data.event ? data.event.id : 0;
}
async function refresh() {
  if(busy) return; busy=true;
  try {
    const status = await api('/api/status'); token=status.token; enabled=status.enabled; settings=status;
    $('phase').textContent = ({starting:'正在启动',checking:'正在检查',ready:'监控正常',waiting_login:'等待独立浏览器登录',error:'需要检查网页',paused:'已暂停'})[status.phase] || status.phase;
    $('message').textContent=status.message; $('dot').className='dot '+status.phase;
    $('last').textContent=date(status.last_check); $('next').textContent=enabled ? date(status.next_check) : '已暂停';
    $('storage').textContent='数据保存在 '+status.data_dir;
    $('toggle').textContent=enabled?'暂停监控':'恢复监控'; $('check').disabled=status.phase==='checking';
    $('mode').value=status.mode;
    if(status.version) $('app-version').textContent='v'+status.version;
    if(status.uid){ $('uid-label').textContent=status.uid; $('space').href=status.space_url; if(document.activeElement!==$('uid')) $('uid').value=status.uid; }
    const auto = status.mode === 'auto';
    $('mode-help').textContent = auto ? '机器自动举报：仅处理有明确原片链接的新投稿；先在独立登录窗口登录。版权投诉会自动选择“路人”、填写出处和说明并提交，认证、验证码或字段变化会保留失败记录供重试。' : '人工审核：发现新投稿后打开记录，核实证据并在 B 站手动提交举报。';
    $('footer-mode').textContent = auto ? '当前为机器自动举报，仅处理明确原片链接；版权投诉会自动填表提交，未确认登录前不会提交举报。' : '当前为人工审核，举报由你在 B 站手动提交。';
    $('browser').textContent = status.browser_open ? '显示独立登录窗口' : '打开独立登录窗口';
    $('browser-ready').disabled = !status.browser_open || status.browser_login_ready;
    $('browser-ready').textContent = status.browser_login_ready ? '已登录，自动举报已启用' : '我已登录，开始举报';
    if(![...$('interval').options].some(option=>Number(option.value)===status.interval)) $('interval').add(new Option(`${status.interval/60} 分钟`,status.interval));
    $('interval').value=status.interval;
    $('coin-watch').checked = !!status.coin_watch;
    if(document.activeElement!==$('report-text-input')) $('report-text-input').value=status.report_text;
    if($('report-placeholders').dataset.ready !== 'yes') { renderPlaceholders(status.report_placeholders); $('report-placeholders').dataset.ready='yes'; }
    render(await api('/api/videos?'+new URLSearchParams({page,q:$('search').value,status:$('filter').value})));
  } catch(error) { $('phase').textContent='本地工具未连接'; $('message').textContent=error.message+'。可重新打开 BiliWatch.exe。'; }
  finally { busy=false; }
}
async function command(action, more={}) { try{await api('/api/control',{action,...more}); await refresh();}catch(error){toast(error.message);} }
async function retryReport(bvid) {
  if (!confirm('重新加入举报队列并再次尝试提交？')) return;
  try {
    await api('/api/control',{action:'retry_report',bvid});
    toast('已重新加入举报队列');
    await refresh();
  } catch(error) { toast(error.message); }
}
function openEditor(video) {
  editing=video; $('editor-title').textContent=video.title; $('edit-status').value=video.status;
  $('edit-url').value=video.evidence_url; $('edit-notes').value=video.notes; $('description').textContent=video.description || '投稿简介为空';
  $('editor').showModal();
}
$('check').onclick=()=>command('check'); $('toggle').onclick=()=>command(enabled?'pause':'resume');
$('mode').onchange=()=>{
  const mode=$('mode').value;
  if(mode==='auto' && !confirm('切换到机器自动举报？仅有明确原片链接的新投稿会自动提交“侵权”举报。')) { refresh(); return; }
  command('mode',{mode});
};
async function saveUid() {
  const uid = $('uid').value.trim();
  if (!/^[1-9][0-9]{0,18}$/.test(uid)) { toast('UID 需为 B 站数字账号 ID，例如 1263732318'); return; }
  const current = $('uid-label').textContent.trim();
  if (uid === current) { toast('当前已在监控该 UID'); return; }
  if (!confirm(`切换到 UID ${uid}？程序会为该账号重新建立监控基线；原 UID 的记录仍会保留，可随时切回。`)) {
    $('uid').value = current; return;
  }
  await command('uid', {uid});
  toast('已切换监控 UID，正在为该账号建立新基线');
}
$('uid-save').onclick=saveUid;
$('uid').onkeydown=event=>{ if(event.key === 'Enter'){ event.preventDefault(); saveUid(); } };
$('browser').onclick=()=>{command('browser');toast('正在打开独立登录窗口；请在该窗口完成 B 站登录。');};
$('browser-ready').onclick=()=>{command('browser_ready');toast('正在检查独立浏览器登录状态。');};
$('interval').onchange=()=>command('interval',{seconds:Number($('interval').value)});
$('coin-watch').onchange=()=>command('coin_watch',{enabled:$('coin-watch').checked});
$('coin-scan').onclick=()=>{command('coin_all');toast('正在读取已存档投稿的投币上限，稍候刷新可见结果。');};
async function probeCoin(bvid) {
  try { await api('/api/control',{action:'coin_probe',bvid}); toast('正在检测 '+bvid+' 的投币上限'); await refresh(); }
  catch(error) { toast(error.message); }
}
function renderPlaceholders(map) {
  const box = $('report-placeholders');
  box.replaceChildren();
  for (const [name, hint] of Object.entries(map || {})) {
    const chip = element('code','{'+name+'}','chip'); chip.title = hint + '（点击插入）';
    chip.addEventListener('click',()=>{
      const field = $('report-text-input'), at = field.selectionStart ?? field.value.length;
      field.setRangeText('{'+name+'}', at, field.selectionEnd ?? at, 'end');
      field.focus();
    });
    box.append(chip, document.createTextNode(' '));
  }
}
$('report-text-open').onclick=()=>{
  if(settings){ $('report-text-input').value = settings.report_text; renderPlaceholders(settings.report_placeholders); }
  $('report-dialog').showModal();
};
$('close-report').onclick=()=>$('report-dialog').close();
$('report-reset').onclick=async()=>{
  try { await api('/api/control',{action:'report_text_reset'}); toast('已恢复内置举报词'); $('report-dialog').close(); await refresh(); }
  catch(error) { toast(error.message); }
};
$('report-form').onsubmit=async event=>{
  event.preventDefault();
  try {
    await api('/api/control',{action:'report_text',text:$('report-text-input').value});
    toast('举报词已保存，将用于后续自动举报'); $('report-dialog').close(); await refresh();
  } catch(error) { toast(error.message); }
};
$('notify').onclick=async()=>{if(!('Notification' in window)){toast('此浏览器不支持桌面通知');return;} const permission=await Notification.requestPermission();toast(permission==='granted'?'已启用桌面提醒，请保持本页面打开':'尚未允许通知，可在浏览器的网站设置里开启');};
$('filter').onchange=()=>{page=1;refresh();}; let searchTimer;
$('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;refresh();},300);};
$('prev').onclick=()=>{page=Math.max(1,page-1);refresh();}; $('next-page').onclick=()=>{page++;refresh();};
$('close-editor').onclick=()=>$('editor').close();
$('evidence-form').onsubmit=async event=>{
  event.preventDefault();
  try{await api('/api/video/'+editing.bvid,{status:$('edit-status').value,evidence_url:$('edit-url').value,notes:$('edit-notes').value});$('editor').close();toast('核实记录已保存');await refresh();}catch(error){toast(error.message);}
};
$('copy').onclick=async()=>{
  const coin = [1,2].includes(editing.copyright) ? (editing.copyright === 1 ? '可投 2 币（稿件标注为自制，疑似转载投自制）' : '仅可投 1 币（稿件标注为转载）') : '未检测';
  const text=[`视频：${editing.title}`,`B 站链接：${editing.url}`,`发布时间：${date(editing.published_at)}`,`原片线索：${editing.source_url || '未找到'}`,`线索来源：${editing.source_kind}`,`版权标注 / 投币上限：${coin}`,`人工证据链接：${$('edit-url').value || '未填写'}`,`核实状态：${labels[$('edit-status').value]}`,`核实笔记：${$('edit-notes').value || '未填写'}`].join('\n');
  try{await navigator.clipboard.writeText(text);toast('证据摘要已复制');}catch(error){toast('复制失败，请允许浏览器访问剪贴板');}
};
$('shutdown').onclick=async()=>{
  if(!confirm('关闭监控程序？保存的投稿与核实记录会保留。'))return;
  try{await api('/api/shutdown',{});clearInterval(poll);$('phase').textContent='工具已关闭';$('message').textContent='再次打开 BiliWatch.exe 可恢复监控。';$('check').disabled=true;}catch(error){toast(error.message);}
};
refresh(); const poll=setInterval(refresh,5000);
