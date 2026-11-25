import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const profile = await mkdtemp(join(tmpdir(), 'projectpilot-retrieval-'))
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
  await command('Page.addScriptToEvaluateOnNewDocument', { source: `window.__calls=[];window.fetch=async function(url,options={}){window.__calls.push({url:String(url),method:options.method||'GET'});return new Response(JSON.stringify({message:'Offline test',error_code:'MOCK_ONLY'}),{status:503})};XMLHttpRequest.prototype.send=function(){throw Error('Offline test blocks XHR')};` })
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
    const {searchPayload}=await import('/src/search.ts');
    const host=document.createElement('div');document.body.replaceChildren(host);const root=createRoot(host);
    const tick=()=>new Promise(resolve=>setTimeout(resolve,60));
    let checks=0;const check=(ok,message)=>{checks++;if(!ok)throw Error(message)};
    const task='offline-task',revision='offline-revision',runId='offline-run';
    const attempt={key:crypto.randomUUID(),payload:searchPayload(revision),run_id:runId};
    sessionStorage.setItem('projectpilot.search.'+task+'.'+revision,JSON.stringify(attempt));
    const hostile='<img src=x onerror="window.__xss=true">';
    const run={task_id:task,confirmed_revision_id:revision,run_id:runId,status:'completed',search_status:'completed',pipeline_status:'completed',plan_status:'published',evidence_status:'not_ready',dispatch_closed:true,retrieval_status:'ready',candidates:[{repository_id:'842846538',collection_status:'completed'}]};
    const chunk={chunk_id:'snap:0:abc',text:hostile,line_start:13,line_end:13,byte_start:0,byte_end:hostile.length,methods:['dense','bm25','keyword'],ranks:{dense:1,bm25:1,keyword:1},dense_distance:0.356,rrf_score:0.04918033,score_direction:'higher_is_better'};
    const requirementA={requirement_item_id:'item-1',field:'required_features',text:'The user must be able to download the transcript as a text file.',modality:'required',operator:'not_applicable',logic_group_id:null};
    const requirementB={requirement_item_id:'item-2',field:'required_technologies',text:'Do not use an LLM in phase one.',modality:'prohibited',operator:'not_applicable',logic_group_id:null};
    const bundle={version:'evidence-bundle-v2',schema_version:2,task_id:task,run_id:runId,confirmed_revision_id:revision,status:'ready',warning:'相关不等于满足',notice:'以下为已保存 README 中的相关片段，只表示“相关”，不代表功能已满足；相似度距离与 RRF 排名不构成满足百分比。',created_at:'2026-01-01T00:00:00+00:00',model:'BAAI/bge-m3',dimension:1024,empty_reason:null,requirements:[requirementA,requirementB],candidates:[{repository_id:'842846538',snapshot_id:'snap',coverage:'full',readme_path:'README.md',commit_sha:'1d0639416ecc6b068d849f78029dc9c2a7594b6b',blob_sha:'f227055633d95d5edd336fd17ab3d26b6f662538',content_hash:'378cf5c3f8e072d37d4037e26c9da727390f82e53cb0bce525c7a92ef9b865b6',collected_at:'2026-11-22T11:00:05+00:00',repository:{full_name:'mikeesto/gemini-transcribe',html_url:'https://github.com/mikeesto/gemini-transcribe',stars:68,description:'Transcribe audio with timestamps',language:'TypeScript'},status:'ok',error_code:null,items:[{requirement_item_id:'item-1',text:requirementA.text,status:'retrieved',relevance:'unverified',empty_reason:null,chunks:[chunk]},{requirement_item_id:'item-2',text:requirementB.text,status:'retrieved',relevance:'unverified',empty_reason:null,chunks:[chunk]}]}]};
    const calls=[];let reads=0;
    window.fetch=async(url,options={})=>{
      calls.push({url:String(url),method:options.method||'GET'});
      if(String(url).endsWith('/retrieval-evidence')) return Response.json(bundle);
      if(String(url).endsWith('/deep-review')) return Response.json({error_code:'DEEP_REVIEW_NOT_FOUND'},{status:404});
      if(String(url).endsWith('/evidence')||String(url).endsWith('/deep-check')) return Response.json({error_code:'NOT_READY'},{status:409});
      reads++;return Response.json(run);
    };
    root.render(React.createElement(Panel,{key:task,taskId:task,revisionId:revision,enabled:true}));await tick();await tick();
    check(reads>=1,'Saved run is read on mount');
    check(calls.some(call=>call.url.endsWith('/retrieval-evidence')&&call.method==='GET'),'Retrieval evidence is fetched with GET only');
    check(calls.every(call=>call.method==='GET'),'No POST while reading retrieval evidence');
    check(host.textContent.includes('mikeesto/gemini-transcribe'),'Repository name rendered');
    check(host.textContent.includes('已召回 1 条候选片段（相关性待核查）'),'Retrieved status is not satisfaction');
    check((host.textContent.match(/<img src=x/g)||[]).length===1,'Duplicate chunk is merged for display');
    check(host.textContent.includes('关联 2 条需求'),'Merged chunk keeps association count');
    check(host.textContent.includes(requirementA.text)&&host.textContent.includes(requirementB.text),'All requirement associations preserved');
    check(host.textContent.includes('未提及不代表符合禁止条件'),'Prohibited requirement warning shown');
    check(host.textContent.includes('README.md')&&host.textContent.includes('行 13'),'Path and line numbers rendered');
    check(host.textContent.includes('dense + bm25 + keyword'),'Retrieval methods rendered');
    check(host.textContent.includes('余弦距离 0.356'),'Cosine distance shown as distance');
    check(!host.textContent.includes('%')&&!host.textContent.includes('满足度'),'No satisfaction percentage');
    check(host.textContent.includes('1d0639416ecc6b068d849f78029dc9c2a7594b6b'),'Real commit_sha rendered');
    check(host.textContent.includes('f227055633d95d5edd336fd17ab3d26b6f662538'),'Real blob_sha rendered');
    check(host.textContent.includes('378cf5c3f8e072d37d4037e26c9da727390f82e53cb0bce525c7a92ef9b865b6'),'Real content_hash rendered');
    check(!host.querySelector('img')&&!window.__xss,'Quotes stay literal text');
    check(calls.every(call=>!call.url.includes('/deep-check')),'No deep check request during retrieval read');
    const partialBundle={...bundle,status:'partial',candidates:[{...bundle.candidates[0],status:'partial',error_code:'DENSE_RETRIEVAL_FAILED',coverage:'truncated',readme_path:null}]};
    const partialRun={...run,retrieval_status:'partial'};
    window.fetch=async(url,options={})=>{
      calls.push({url:String(url),method:options.method||'GET'});
      if(String(url).endsWith('/retrieval-evidence')) return Response.json(partialBundle);
      if(String(url).endsWith('/deep-review')) return Response.json({error_code:'DEEP_REVIEW_NOT_FOUND'},{status:404});
      if(String(url).endsWith('/evidence')||String(url).endsWith('/deep-check')) return Response.json({error_code:'NOT_READY'},{status:409});
      return Response.json(partialRun);
    };
    root.render(React.createElement(Panel,{key:task+'b',taskId:task,revisionId:revision,enabled:true}));await tick();await tick();
    check(host.textContent.includes('部分仓库或需求未能完成检索'),'Partial run status explained');
    check(host.textContent.includes('证据覆盖不完整'),'Truncated coverage explained');
    check(host.textContent.includes('历史记录未保存文件路径'),'Missing historical path is explicit');
    check(!host.textContent.includes('README.md'),'Historical bundle never fakes README.md');
    const legacy={...partialBundle,schema_version:1,candidates:[{...partialBundle.candidates[0],commit_sha:null,blob_sha:null,content_hash:null,collected_at:null}]};
    window.fetch=async(url,options={})=>{
      if(String(url).endsWith('/retrieval-evidence')) return Response.json(legacy);
      if(String(url).endsWith('/deep-review')) return Response.json({error_code:'DEEP_REVIEW_NOT_FOUND'},{status:404});
      if(String(url).endsWith('/evidence')||String(url).endsWith('/deep-check')) return Response.json({error_code:'NOT_READY'},{status:409});
      return Response.json(partialRun);
    };
    root.render(React.createElement(Panel,{key:task+'e',taskId:task,revisionId:revision,enabled:true}));await tick();await tick();
    check(host.textContent.includes('历史记录未保存完整快照标识'),'v1 bundle trace fallback is explicit');
    const emptyBundle={...bundle,status:'ready',candidates:[{...bundle.candidates[0],items:[{...bundle.candidates[0].items[0],status:'empty',chunks:[]}]}]};
    window.fetch=async(url,options={})=>{
      if(String(url).endsWith('/retrieval-evidence')) return Response.json(emptyBundle);
      if(String(url).endsWith('/deep-review')) return Response.json({error_code:'DEEP_REVIEW_NOT_FOUND'},{status:404});
      if(String(url).endsWith('/evidence')||String(url).endsWith('/deep-check')) return Response.json({error_code:'NOT_READY'},{status:409});
      return Response.json(run);
    };
    root.render(React.createElement(Panel,{key:task+'d',taskId:task,revisionId:revision,enabled:true}));await tick();await tick();
    check(host.textContent.includes('未召回候选片段'),'Empty item state is explicit');
    const failed={...run,retrieval_status:'failed',retrieval_error_code:'RETRIEVAL_FAILED'};
    let retrievalFetches=0;
    window.fetch=async(url,options={})=>{
      calls.push({url:String(url),method:options.method||'GET'});
      if(String(url).endsWith('/retrieval-evidence')) retrievalFetches++;
      if(String(url).endsWith('/deep-review')) return Response.json({error_code:'DEEP_REVIEW_NOT_FOUND'},{status:404});
      if(String(url).endsWith('/evidence')||String(url).endsWith('/deep-check')) return Response.json({error_code:'NOT_READY'},{status:409});
      return Response.json(failed);
    };
    root.render(React.createElement(Panel,{key:task+'c',taskId:task,revisionId:revision,enabled:true}));await tick();await tick();
    check(host.textContent.includes('检索证据生成失败'),'Failed retrieval is visible');
    check(retrievalFetches===0,'Failed retrieval is not fetched on GET');
    check(host.textContent.includes('检索证据尚未生成')===false,'Failed state is not shown as not started');
    root.unmount();return {passed:checks,requests:calls.length,externalRequests:0};
  })()`)
  console.log(JSON.stringify(result))
  assert.equal(result.passed, 27)
  await send('Browser.close')
} finally {
  socket?.close()
  if (browser.exitCode === null) await new Promise(resolve => { browser.once('exit', resolve); setTimeout(() => { browser.kill(); resolve() }, 3000).unref() })
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
}
