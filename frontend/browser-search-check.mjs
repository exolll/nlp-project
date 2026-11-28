import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const profile = await mkdtemp(join(tmpdir(), 'projectpilot-browser-'))
const browser = spawn('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'])
let socket
try {
  const endpoint = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('Browser startup timed out')), 20000)
    browser.stderr.on('data', chunk => {
      const match = String(chunk).match(/DevTools listening on (ws:\/\/[^\s]+)/)
      if (match) { clearTimeout(timeout); resolve(match[1]) }
    })
    browser.on('error', reject)
  })
  socket = new WebSocket(endpoint)
  await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }))
  let sequence = 0
  const pending = new Map()
  socket.addEventListener('message', event => {
    const data = JSON.parse(event.data)
    if (pending.has(data.id)) {
      const { resolve, reject } = pending.get(data.id)
      pending.delete(data.id)
      data.error ? reject(Error(JSON.stringify(data.error))) : resolve(data.result)
    }
  })
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const id = ++sequence
    pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }))
  })
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
  const command = (method, params) => send(method, params, sessionId)
  await command('Page.enable')
  await command('Page.addScriptToEvaluateOnNewDocument', { source: `window.__calls=[];window.__errors=[];window.addEventListener('error',event=>window.__errors.push(String(event.error||event.message)));window.addEventListener('unhandledrejection',event=>window.__errors.push(String(event.reason)));window.fetch=async function(url,options={}){window.__calls.push({url:String(url),method:options.method||'GET'});return new Response(JSON.stringify({message:'Offline test',error_code:'MOCK_ONLY'}),{status:503})};XMLHttpRequest.prototype.send=function(){throw Error('Offline test blocks XHR')};` })
  await command('Page.navigate', { url: 'http://127.0.0.1:5173' })
  const evaluate = async expression => {
    const result = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
  await evaluate(`new Promise(resolve=>{if(document.readyState==='complete')resolve();else window.addEventListener('load',resolve,{once:true})})`)
  const result = await evaluate(`(async()=>{
    const {default:React}=await import('/node_modules/.vite/deps/react.js');
    const {default:{createRoot}}=await import('/node_modules/.vite/deps/react-dom_client.js');
    const {default:Panel}=await import('/src/SearchPanel.tsx');
    const host=document.createElement('div');document.body.replaceChildren(host);const root=createRoot(host);
    const tick=()=>new Promise(resolve=>setTimeout(resolve,50));
    let checks=0;const check=(ok,message)=>{checks++;if(!ok)throw Error(message)};
    const task='offline-task',revision='offline-revision',runId='offline-run';
    let mode='unknown',deepFail=false,deepMode='off',reviewFail=false,reviewMode='off',reads=0;
    const calls=[];
    const run={task_id:task,confirmed_revision_id:revision,run_id:runId,status:'failed',search_status:'partial',pipeline_status:'running',plan_status:'published',evidence_status:'not_ready',retrieval_status:'ready',dispatch_closed:true,candidates:[{repository_id:'10',collection_status:'completed'}]};
    const requirement={requirement_item_id:'item',text:'Audio export',modality:'required',operator:'not_applicable',logic_group_id:null};
    const hostile='<img src=x onerror="window.__xss=true">';
    const report={task_id:task,confirmed_revision_id:revision,run_id:runId,warning:'Literal mentions only',requirement_items:[requirement],candidates:[{repository_id:'10',availability:'available',source:{snapshot_id:'snapshot',commit_sha:'commit',blob_sha:'blob',content_hash:'hash',readme_path:'README.md'},items:[{...requirement,status:'mention_found',quotes:[{text:hostile,byte_start:0,byte_end:hostile.length,line_start:1,line_end:1}]}]},{repository_id:'20',availability:'no_snapshot',source:null,items:[{...requirement,status:'unconfirmed',quotes:[]}]}]};
    const deepReport={version:'deep-check-v1',task_id:task,run_id:runId,confirmed_revision_id:revision,notice:'模型分析不代表需求已被满足',requirement_items:[requirement],anomalies:[],candidates:[{repository_id:'10',rank:0,availability:'snapshot',availability_label:'已保存快照',input_coverage:'full',input_coverage_label:'全文已送入',input_bytes:21,source:{snapshot_id:'snapshot',commit_sha:'commit',readme_path:'README.md',blob_sha:'blob',content_hash:'hash'},recommendation:{code:'verify_further',label:'值得进一步核验',reason_code:'all_hard_requirements_supported',reason:'全部硬性需求均找到支持证据'},summary:'硬性需求 1/1 项有证据支持，0 项信息不足。',missing_features:[],risk_notes:[],items:[{...requirement,verdict:'supports',basis:'model',rejections:[],verdict_label:'有证据支持',basis_label:'模型判定且引文通过复核',citations:[{text:hostile,line_start:1,line_end:1,byte_start:0,byte_end:hostile.length}]}]}]};
    const reviewOperation={operation_id:'review-operation',status:'running',persisted_status:'running',revision_id:revision,revision_hash:'revision-hash',bundle_hash:'bundle-hash',model:'mock-model',prompt_version:'deep-review-v1',consent:true,authorized_at:'2026-01-01T00:00:00Z',deadline:'2026-01-01T00:10:00Z',report_key:null,report_hash:null,error_code:null,report:null};
    const reviewReport={schema_version:1,version:'deep-review-v1',task_id:task,run_id:runId,confirmed_revision_id:revision,operation_id:'review-operation',revision_hash:'revision-hash',plan_hash:'plan-hash',bundle_hash:'bundle-hash',model:'mock-model',prompt_version:'deep-review-v1',notice:'本报告仅基于已保存的 README 文档证据，不代表源码审计、代码运行验证或部署可用性。',created_at:'2026-01-01T00:00:00Z',status:'completed',repositories:[{repository_id:'10',snapshot_id:'review-snapshot',trace:{commit_sha:'review-commit',blob_sha:'review-blob',content_hash:'review-content',readme_path:'README.md',collected_at:'2026-01-01T00:00:00Z'},coverage:'full',status:'completed',error_code:null,input:{chunk_ids:['review-chunk'],chunk_count:1,input_bytes_upper_bound:21,budget:{max_chunks:16,max_input_bytes:24000,max_output_tokens:2000}},requirements:[{...requirement,field:'required_features',verdict:'supports',reason_code:'supported_by_citation',hard_constraint_check:'pass',model_omitted:false,rejected_citations:[],citations:[{chunk_id:'review-chunk',quote:hostile,line_start:2,line_end:2,byte_start:0,byte_end:hostile.length}]}],hard_constraint_summary:{required:{pass:1,fail:0,unknown:0,total:1},prohibited:{pass:0,fail:0,unknown:0,total:0},preferred:{evidence_found:0,unknown:0,total:0}},logic_group_results:[],usage:{known:true,prompt_tokens:7,completion_tokens:3,total_tokens:10},response_hash:'response-hash'}],summary:{repositories_total:1,repositories_completed:1,repositories_failed:0,repositories_unknown:0,repositories_skipped:0,requirements_total:1,supports:1,contradicts:0,insufficient:0,model_omitted:0,duplicates:0,unknown_items:0,usage:{calls:1,unknown_calls:0,prompt_tokens:7,completion_tokens:3,total_tokens:10}},warnings:[]};
    window.fetch=async(url,options={})=>{
      calls.push({url:String(url),method:options.method||'GET',key:options.headers?.['Idempotency-Key'],body:options.body});
      const review=String(url).endsWith('/deep-review');
      const deep=String(url).endsWith('/deep-check');
      if(review&&options.method==='POST'){if(reviewFail)throw Error('Simulated lost review response');return Response.json(reviewOperation,{status:202})}
      if(review){if(reviewMode==='off')return Response.json({error_code:'DEEP_REVIEW_NOT_FOUND',message:'Not authorized'},{status:404});return Response.json(reviewMode==='ready'?{...reviewOperation,status:'completed',persisted_status:'completed',report:reviewReport,report_key:'deep-reviews/review-operation/report.json',report_hash:'report-hash'}:reviewOperation)}
      if(options.method==='POST'){if(deep?deepFail:mode==='unknown')throw Error('Simulated lost response');return Response.json(deep?{...run,deep_check:{status:'dispatching',published:false,integrity_blocked:false}}:run)}
      if(deep)return Response.json(deepReport);
      if(String(url).endsWith('/retrieval-evidence'))return Response.json({notice:'Offline retrieval evidence',empty_reason:null,requirements:[],candidates:[]});
      if(String(url).endsWith('/evidence'))return Response.json(report);
      reads++;return Response.json({...run,...(mode==='published'?{pipeline_status:'completed',evidence_status:'published'}:mode==='interrupted'?{pipeline_status:'interrupted'}:{}),...(deepMode==='off'?{}:{deep_check:deepMode==='ready'?{status:'completed',published:true,integrity_blocked:false}:{status:'dispatching',published:false,integrity_blocked:false}})});
    };
    const render=(id=task,enabled=true)=>root.render(React.createElement(Panel,{key:id,taskId:id,revisionId:revision,enabled}));
    const buttons=()=>[...host.querySelectorAll('button')];
    render();await tick();check(buttons()[0].disabled,'Consent must disable submit');check(calls.length===0,'No automatic POST or GET');
    host.querySelector('input[type=checkbox]').click();await tick();check(!buttons()[0].disabled,'Consent enables submit');
    buttons()[0].click();await tick();check(calls.length===1,'Unknown POST must not retry');check(host.textContent.includes('原载荷与 UUID 已保留'),'Unknown outcome explained');
    mode='partial';buttons()[0].click();await tick();check(calls[0].key===calls[1].key&&calls[0].body===calls[1].body,'Explicit retry keeps UUID/payload');check(host.textContent.includes('仅有部分采集结果'),'Backend failed+partial status explained: '+host.textContent+' errors: '+JSON.stringify(window.__errors));
    check(JSON.parse(calls[0].body).budget.github_requests===20,'Exact budget');
    mode='published';buttons()[1].click();await tick();check(host.textContent.includes('发现字面提及')&&host.textContent.includes('尚未确认'),'Evidence verdicts');
    check(host.textContent.includes(hostile)&&!host.querySelector('img')&&!window.__xss,'Quotes must be literal safe text');
    const after=calls.length;await new Promise(resolve=>setTimeout(resolve,2800));check(calls.length===after,'Completed pipeline stops polls');
    const boxes=()=>[...host.querySelectorAll('input[type=checkbox]')];
    const deepPosts=()=>calls.filter(call=>call.method==='POST'&&call.url.includes('/deep-check'));
    check(boxes().length===3&&boxes()[1].checked===false&&boxes()[2].checked===false,'Each deep authorization keeps its own unchecked consent');
    check(boxes()[1].disabled===false&&buttons()[2].disabled,'Deep POST stays disabled until its own consent');
    boxes()[1].click();await tick();check(!buttons()[2].disabled,'Deep consent enables only the deep POST');
    deepFail=true;const beforeDeep=calls.length;buttons()[2].click();await tick();
    check(calls.length===beforeDeep+1&&deepPosts().length===1,'Deep unknown outcome never auto-retries');
    check(host.textContent.includes('仅允许按钮重发同一次授权'),'Deep unknown outcome explained without inventing a run');
    const firstDeep=deepPosts()[0];buttons()[2].click();await tick();
    check(deepPosts()[1].key===firstDeep.key&&deepPosts()[1].body===firstDeep.body,'Explicit deep retry keeps UUID and exact payload');
    check(JSON.parse(firstDeep.body).consent===true&&JSON.parse(firstDeep.body).revision_id===revision,'Deep payload carries its own consent and the immutable revision');
    check(!host.textContent.includes('值得进一步核验'),'No deep report invented before the backend publishes');
    deepFail=false;deepMode='live';buttons()[3].click();await new Promise(resolve=>setTimeout(resolve,2800));
    check(host.textContent.includes('深度核对正在进行'),'Deep progress comes from GET polling only');
    check(buttons()[2].disabled,'A live deep check cannot be re-authorized');
    deepMode='ready';await new Promise(resolve=>setTimeout(resolve,2800));
    check(host.textContent.includes('值得进一步核验')&&host.textContent.includes('有证据支持')&&host.textContent.includes('硬性需求 1/1'),'Chinese recommendation, verdict and deterministic summary rendered');
    check(host.textContent.includes('不代表需求已被满足')&&host.textContent.includes('已保存快照'),'Non-satisfaction notice and snapshot availability rendered');
    check(!!host.querySelector('.deep-report')&&!host.querySelector('.deep-report img')&&!window.__xss,'Deep citations stay literal safe text');
    check(deepPosts().length===2&&calls.filter(call=>call.method==='POST').length===4,'Publication polling never re-sends the paid call');
    check(buttons()[2].disabled&&buttons()[2].textContent==='本次运行已授权深度核对','Published run locks a second authorization for the same run');
    const reviewPosts=()=>calls.filter(call=>call.method==='POST'&&call.url.includes('/deep-review'));
    check(boxes()[2].disabled===false&&buttons()[4].disabled,'Deep review stays disabled until its independent consent');
    check(host.textContent.includes('不会直接上传原始 PDF/Word 文件')&&host.textContent.includes('系统将按仓库最多发起 5 次模型调用'),'Fixed deep-review data, purpose and cost consent is visible');
    boxes()[2].click();await tick();check(!buttons()[4].disabled,'Deep-review consent enables only its own POST');
    reviewFail=true;buttons()[4].click();await tick();
    check(reviewPosts().length===1&&reviewPosts()[0].key===undefined,'Unknown deep-review POST is sent once without an idempotency key');
    check(JSON.parse(reviewPosts()[0].body).consent===true&&JSON.parse(reviewPosts()[0].body).revision_id===revision,'Deep-review POST carries explicit consent and immutable revision');
    reviewFail=false;reviewMode='live';buttons()[4].click();await tick();
    check(reviewPosts().length===2&&reviewPosts()[0].body===reviewPosts()[1].body,'Explicit deep-review retry keeps the exact payload and backend operation identity');
    check(host.textContent.includes('逐仓库深度核查正在进行')&&calls.filter(call=>call.method==='GET'&&call.url.includes('/deep-review')).length>=1,'Deep-review progress comes from GET only');
    const afterReviewPosts=reviewPosts().length;await new Promise(resolve=>setTimeout(resolve,2800));check(reviewPosts().length===afterReviewPosts,'Deep-review polling never re-sends POST');
    reviewMode='ready';await new Promise(resolve=>setTimeout(resolve,2800));
    check(host.textContent.includes('逐仓库深度核查（MVP）')&&host.textContent.includes('有证据支持'),'Published deep-review status and verdict render');
    check(host.textContent.includes('README.md · 行 2–2')&&host.textContent.includes('review-snapshot')&&host.textContent.includes('review-commit'),'README path, lines and snapshot identity render');
    check(host.textContent.includes('输入 7 · 输出 3 · 合计 10')&&host.textContent.includes('模型调用次数'),'Provider token usage and call count render');
    check(host.textContent.includes('不代表源码审计、代码运行验证或部署可用性'),'Deep-review evidence limitation notice renders');
    check(![...host.querySelectorAll('.deep-report img')].length&&!window.__xss,'Deep-review citations remain literal safe text');
    check(buttons()[4].disabled&&buttons()[4].textContent==='本次运行已授权深度核查','Published deep review locks another authorization for the same run');
    check(reviewPosts().length===2,'Publication polling never re-sends the paid deep-review call');
    render('offline-other');await tick();check(!host.textContent.includes(runId),'Task change clears stale evidence');check(buttons()[0].disabled,'New task needs new consent');
    render(task);await tick();check(calls.filter(call=>call.method==='POST'&&!call.url.includes('/deep-check')&&!call.url.includes('/deep-review')).length===2,'Remount only reads saved run');
    mode='interrupted';buttons()[1].click();await tick();const stopped=calls.length;await new Promise(resolve=>setTimeout(resolve,2800));check(calls.length===stopped,'Interrupted pipeline stops polls');
    render('offline-disabled',false);await tick();check(host.querySelector('input').disabled&&buttons()[0].disabled,'Unsaved edits prevent search');
    const {default:Requirements}=await import('/src/RequirementsPanel.tsx');
    const confirmed={revision_id:'integration-revision',task_id:'integration-task',revision_number:1,parent_revision_id:null,status:'confirmed',created_at:'2026-11-22T00:00:00Z',confirmed_at:'2026-11-22T00:00:01Z',change_log:[],requirements:{project_topic:'Audio export',project_description:null,project_type:null,domain:null,required_features:null,preferred_features:null,programming_languages:null,required_technologies:null,preferred_technologies:null,missing_information:[],uncertain_information:null,source_evidence:[]},requirement_items:[{...requirement,field:'project_topic',value_path:'project_topic',source_kind:'user_primary_text',evidence_ids:[],supersedes_item_id:null}]};
    window.fetch=async(url,options={})=>{calls.push({url:String(url),method:options.method||'GET'});check(String(url).endsWith('/requirements')&&!options.body,'Integration only reads requirements');return Response.json({extraction:null,revisions:[confirmed],latest_revision_id:confirmed.revision_id})};
    root.render(React.createElement(Requirements,{document:{document_id:'integration-document',task_id:'integration-task',source_segments:[]}}));await tick();await tick();
    check(!!host.querySelector('.search-panel'),'Confirmed requirements expose search panel');
    check(host.querySelector('#requirement-project_topic').disabled,'Confirmed requirement remains immutable');
    [...host.querySelectorAll('button')].find(button=>button.textContent==='新建修订草稿').click();await tick();
    check(!host.querySelector('#requirement-project_topic').disabled,'Existing draft editing works');
    check(host.querySelector('.search-panel input').disabled,'Editing requirement disables search consent');
    check(calls.filter(call=>call.method==='POST').length===6,'Integration never auto-dispatches');
    root.unmount();return {passed:checks,requests:calls.length,posts:calls.filter(call=>call.method==='POST').length,externalRequests:0};
  })()`)
  console.log(JSON.stringify(result))
  assert.equal(result.passed, 52)
  await send('Browser.close')
} finally {
  socket?.close()
  if (browser.exitCode === null) await new Promise(resolve => { browser.once('exit', resolve); setTimeout(() => { browser.kill(); resolve() }, 3000).unref() })
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
}
