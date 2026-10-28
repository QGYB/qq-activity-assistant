// SPDX-License-Identifier: AGPL-3.0-only
// SPDX-FileCopyrightText: 2026 QGYB and contributors
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {extractMessages,observeNativeExports,reconcileRuntime,createTransport}=require('./preload.cjs');
const http=require('node:http');

async function localBridge(t,handler){
 const received=[];
 const server=http.createServer((request,response)=>{
  let body='';request.on('data',chunk=>body+=chunk);request.on('end',()=>{
   const event=JSON.parse(body);received.push(event);
   if(handler)handler(event,response,received.length);else{response.writeHead(200);response.end('{}');}
  });
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 t.after(()=>new Promise(resolve=>{server.close(resolve);server.closeAllConnections();}));
 return {received,config:{host:'127.0.0.1',port:server.address().port,token:'t'.repeat(32),groups:['100001234']}};
}
async function waitUntil(predicate,timeout=3000){
 const end=Date.now()+timeout;
 while(!predicate()){if(Date.now()>end)throw Error('Timed out waiting for local transport');await new Promise(resolve=>setTimeout(resolve,10));}
}
function notice(index,text='活动通知'){return {chatType:2,peerUin:'100001234',msgId:String(index),msgTime:1793174400,elements:[{textElement:{content:text}}]};}

test('image-only notices in configured groups remain visible for manual completion',()=>{
 const [event]=extractMessages([{chatType:2,peerUin:'100001234',msgId:'image',elements:[{picElement:{}}]}],['100001234']);
 assert.ok(event);assert.equal(event.image_only,true);assert.match(event.text,/图片/);
});

test('only configured group messages leave the native callback',()=>{
 const records=[{chatType:2,peerUin:'100001234',msgId:'a',msgTime:'1793174400',elements:[{textElement:{content:'【文明岗活动】'}}]}, {chatType:1,peerUin:'100001234',msgId:'private',elements:[{textElement:{content:'private'}}]}, {chatType:2,peerUin:'123456',msgId:'other',elements:[{textElement:{content:'other'}}]}];
 const events=extractMessages(records,['100001234']);
 assert.equal(events.length,1);assert.equal(events[0].text,'【文明岗活动】');assert.equal(events[0].group_id,'100001234');assert.equal(events[0].message_id,'a');
 assert.equal(extractMessages(records,[]).length,0);
 assert.deepEqual(Object.keys(events[0]).sort(),['group_id','message_id','text','time','type'].sort());
});

test('text and structured card titles and WPS URL are retained',()=>{
 const r={chatType:2,peerUin:'100001234',msgId:'b',elements:[{textElement:{content:'【活动链接】'}},{arkElement:{bytesData:JSON.stringify({meta:{news:{title:'信智天宫庄园',jumpUrl:'https://f.wps.cn/g/DemoGardenForm/'}}})}}]};
 const [event]=extractMessages([r],['100001234']);assert.match(event.text,/信智天宫庄园/);assert.match(event.text,/https:\/\/f\.wps\.cn\/g\/DemoGardenForm\//);
});

test('observer preserves existing QQ listener and method receiver',()=>{
 let listener,originalCount=0,serviceThis,callbackThis;
 const service={addKernelMsgListener(l){serviceThis=this;listener=l;return 'ok';}};
 const session={getMsgService(){assert.equal(this,session);return service;}};
 const api={NodeIQQNTWrapperSession:{create(){assert.equal(this,api.NodeIQQNTWrapperSession);return session;}}};
 const emitted=[];const hooked=observeNativeExports(api,e=>emitted.push(e),()=>['100001234']);
 const existing={onRecvMsg(records){callbackThis=this;originalCount++;return records.length;},onDraftUpdate(){return 88;}};
 assert.equal(hooked.NodeIQQNTWrapperSession.create().getMsgService().addKernelMsgListener(existing),'ok');
 const records=[{chatType:2,peerUin:'100001234',msgId:'c',elements:[{textElement:{content:'活动'}}]}];
 assert.equal(listener.onRecvMsg(records),1);assert.equal(originalCount,1);assert.equal(serviceThis,service);assert.equal(callbackThis,existing);assert.equal(listener.onDraftUpdate(),88);assert.equal(emitted.filter(e=>e.type==='message').length,1);
});

test('bridge failures never interrupt the QQ callback',()=>{
 let listener,called=false;
 const api={NodeIQQNTWrapperSession:{create:()=>({getMsgService:()=>({addKernelMsgListener:l=>{listener=l;}})})}};
 const hooked=observeNativeExports(api,()=>{throw Error('bridge down');},()=>['100001234']);
 hooked.NodeIQQNTWrapperSession.create().getMsgService().addKernelMsgListener({onRecvMsg(){called=true;return 42;}});
 assert.equal(listener.onRecvMsg([{chatType:2,peerUin:'100001234',msgId:'d',elements:[{textElement:{content:'a'}}]}]),42);assert.equal(called,true);
});

test('new QQ session lookup factories are observed without creating sessions',()=>{
 let listener,lookups=0;
 const session={getMsgService:()=>({addKernelMsgListener:l=>{listener=l;}})};
 const api={NodeIQQNTWrapperSession:{getNTWrapperSession(name){assert.equal(this,api.NodeIQQNTWrapperSession);assert.equal(name,'nt_1');lookups++;return session;}}};
 const events=[];const hooked=observeNativeExports(api,e=>events.push(e),()=>['100001234']);
 assert.equal(lookups,0);
 hooked.NodeIQQNTWrapperSession.getNTWrapperSession('nt_1').getMsgService().addKernelMsgListener({onRecvMsg(){}});
 assert.equal(lookups,1);assert.equal(typeof listener?.onRecvMsg,'function');
 listener.onRecvMsg([{chatType:2,peerUin:'100001234',msgId:'new',elements:[{textElement:{content:'活动'}}]}]);
 assert.equal(events.filter(e=>e.type==='message').length,1);
});

test('unrelated native class exports retain identity and static login/engine methods',()=>{
 class LoginService {static get(){return 'original-login';}}
 class Engine {static get(){return 'original-engine';}}
 const api={NodeIKernelLoginService:LoginService,NodeIQQNTWrapperEngine:Engine,NodeIQQNTWrapperSession:{getNTWrapperSession(){return {getMsgService(){return {};}};}}};
 const observed=observeNativeExports(api,()=>{},()=>[]);
 assert.equal(observed.NodeIKernelLoginService,LoginService);
 assert.equal(observed.NodeIKernelLoginService.get(),'original-login');
 assert.equal(observed.NodeIQQNTWrapperEngine.get(),'original-engine');
});

test('native session constructors preserve new semantics and are still observed',()=>{
 let listener;
 class NativeSession {constructor(value){this.value=value;}getMsgService(){assert.equal(this.value,'gui');return {addKernelMsgListener:l=>{listener=l;}};}}
 NativeSession.getNTWrapperSession=()=>undefined;
 const events=[];
 const observed=observeNativeExports({NodeIQQNTWrapperSession:NativeSession},e=>events.push(e),()=>['100001234']);
 const session=new observed.NodeIQQNTWrapperSession('gui');
 assert.ok(session instanceof NativeSession);
 session.getMsgService().addKernelMsgListener({onRecvMsg(){}});
 listener.onRecvMsg([{chatType:2,peerUin:'100001234',msgId:'ctor',elements:[{textElement:{content:'活动'}}]}]);
 assert.equal(events.filter(e=>e.type==='message').length,1);
});

test('background polling checks only configured groups and deduplicates each time slot',async()=>{
 const realNow=Date.now;let clock=1800000000000;Date.now=()=>clock;
 try{
  const calls=[],received=[];
  const service={addKernelMsgListener(){},async getAioFirstViewLatestMsgs(peer,count){calls.push([peer,count]);return {result:0,msgList:[{chatType:2,peerUin:'100001234',msgId:String(calls.length),elements:[{textElement:{content:'活动通知'}}]}]};}};
  const runtime={nativeApi:{NodeIQQNTWrapperSession:{getNTWrapperSession:()=>({getMsgService:()=>service})}},sessionChecks:0,historyCount:0,historyState:'idle',getConfig:()=>({groups:['100001234'],poll_seconds:15}),emit(){},debug(){},onRecords:records=>received.push(...records)};
  reconcileRuntime(runtime);await new Promise(setImmediate);
  assert.equal(calls.length,1);assert.deepEqual(calls[0][0],{chatType:2,peerUid:'100001234'});
  reconcileRuntime(runtime);await new Promise(setImmediate);assert.equal(calls.length,1);
  clock+=15000;reconcileRuntime(runtime);await new Promise(setImmediate);
  assert.equal(calls.length,2);assert.equal(received.length,2);
 }finally{Date.now=realNow;}
});

test('serial HTTP drain delivers repeated 20-message batches quickly and preserves content revisions',async t=>{
 const {received,config}=await localBridge(t);
 const transport=createTransport(()=>config);t.after(()=>transport.close());
 const started=Date.now();
 for(let batch=0;batch<3;batch++){
  const events=extractMessages(Array.from({length:20},(_,i)=>notice(batch*20+i)),config.groups);
  for(let repeat=0;repeat<4;repeat++)for(const event of events)transport.emit({...event,delivery:'history'});
  transport.emit({type:'heartbeat',state:'listening',revision:batch});
 }
 const [revision]=extractMessages([notice(4,'修订后的活动通知')],config.groups);
 transport.emit({...revision,delivery:'history'});
 assert.equal(received.length,0,'enqueue does not wait for HTTP on the QQ callback stack');
 await waitUntil(()=>transport.pending()===0);
 assert.ok(Date.now()-started<3000);
 assert.equal(received.filter(event=>event.type==='message').length,61);
 assert.equal(transport.stats.deduplicated,180);
 assert.deepEqual(received.filter(event=>event.message_id==='4').map(event=>event.text),['活动通知','修订后的活动通知']);
 const heartbeatIndex=received.findIndex(event=>event.type==='heartbeat');
 assert.ok(heartbeatIndex>=0&&heartbeatIndex<=10,'heartbeat gets a turn during a full message burst');
 assert.equal(received[heartbeatIndex].revision,2,'queued heartbeats merge to newest state');
});

test('bounded overload preserves a heartbeat slot and HTTP errors retry with backoff',async t=>{
 const attempts=[];
 const {received,config}=await localBridge(t,(_event,response,count)=>{
  attempts.push(Date.now());response.writeHead(count<3?503:200);response.end('{}');
 });
 const transport=createTransport(()=>config,{maxQueue:25,retryBase:25});t.after(()=>transport.close());
 for(const event of extractMessages(Array.from({length:100},(_,i)=>notice(i)),config.groups))transport.emit(event);
 transport.emit({type:'heartbeat',state:'listening'});
 assert.ok(transport.pending()<=26);
 await waitUntil(()=>transport.pending()===0);
 assert.ok(attempts[1]-attempts[0]>=20,'failed requests are not spun in a tight loop');
 assert.ok(attempts[2]-attempts[1]>=40,'repeated failures increase retry delay');
 assert.ok(received.some(event=>event.type==='heartbeat'));
 assert.equal(transport.stats.failed,2);assert.equal(transport.stats.dropped,75);
});

test('hot reconcile upgrades an existing closure once without attaching or creating native sessions',async t=>{
 const {received,config}=await localBridge(t);
 let attaches=0,lookups=0,oldEmits=0;
 const service={addKernelMsgListener(){attaches++;throw Error('must retain old listener');}};
 const session={getMsgService(){throw Error('must retain old service');}};
 const runtime={nativeApi:{NodeIQQNTWrapperSession:{getNTWrapperSession(){lookups++;throw Error('must retain old session');}}},
  session,service,historyState:'complete',historyCount:900,getConfig:()=>config,emit(){oldEmits++;},onRecords(){throw Error('old backlog');},extract:extractMessages};
 const listener={onRecvMsg:records=>runtime.onRecords(records)};runtime.listener=listener;
 reconcileRuntime(runtime);const first=runtime.transport;t.after(()=>first.close());
 reconcileRuntime(runtime);assert.equal(runtime.transport,first);
 assert.equal(runtime.listener,listener);assert.equal(runtime.session,session);assert.equal(runtime.service,service);
 for(let i=0;i<3;i++)listener.onRecvMsg(Array.from({length:20},(_,n)=>notice(n)));
 assert.equal(received.length,0,'native callback returns before network I/O completes');
 await waitUntil(()=>first.pending()===0);
 assert.equal(attaches,0);assert.equal(lookups,0);assert.equal(oldEmits,0);
 assert.equal(received.filter(event=>event.type==='message').length,20);
 assert.equal(received.find(event=>event.type==='heartbeat').state,'listening');
 runtime.lastTransportHeartbeat=Date.now()-6000;reconcileRuntime(runtime);
 await waitUntil(()=>received.filter(event=>event.type==='heartbeat').length===2);
 assert.equal(received.at(-1).callback_count,60);
});
