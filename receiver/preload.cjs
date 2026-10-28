// SPDX-License-Identifier: AGPL-3.0-only
// SPDX-FileCopyrightText: 2026 QGYB and contributors
'use strict';
// Independent, read-only observer for the existing QQ session's message callbacks.
// No bot framework is loaded and original QQ callbacks keep their receiver/return value.
const fs=require('fs');
const http=require('http');
const crypto=require('crypto');

function cardStrings(value,out=[],depth=0){
 if(depth>8||out.length>80||!value)return out;
 if(Array.isArray(value)){for(const v of value)cardStrings(v,out,depth+1);return out;}
 if(typeof value==='object')for(const [key,v] of Object.entries(value)){
  if(typeof v==='string'&&/^(title|desc|prompt|url|jumpurl|qqdocurl|content)$/i.test(key))out.push(v);
  else if(v&&typeof v==='object')cardStrings(v,out,depth+1);
 }
 return out;
}
function elementText(element){
 if(element?.textElement?.content)return String(element.textElement.content);
 const raw=element?.arkElement?.bytesData||element?.jsonElement?.content;
 if(typeof raw==='string'){try{return cardStrings(JSON.parse(raw)).join('\n');}catch{return '';}}
 return '';
}
function extractMessages(records,groups){
 const allow=new Set((groups||[]).map(String));
 if(!Array.isArray(records)||!allow.size)return [];
 const result=[];
 for(const record of records){
  if(Number(record?.chatType)!==2)continue;
  const group=String(record.peerUin||record.groupCode||record.peerUid||'');
  if(!allow.has(group))continue;
  const elements=Array.isArray(record.elements)?record.elements:[];
  let text=elements.map(elementText).filter(Boolean).join('\n').slice(0,65536);
  const imageOnly=!text.trim()&&elements.some(e=>e.picElement);
  if(imageOnly)text='【待核对图片通知】\n活动信息在图片中，请补充活动文字和报名链接。';
  if(!text.trim())continue;
  const time=Number(record.msgTime)||Math.floor(Date.now()/1000);
  const id=String(record.msgId||record.msgSeq||crypto.createHash('sha256').update(group+'|'+time+'|'+text).digest('hex'));
  const event={type:'message',group_id:group,message_id:id,time:time>1e12?Math.floor(time/1000):time,text};
  if(imageOnly)event.image_only=true;
  result.push(event);
 }
 return result;
}
function forwardingProxy(original,overrides,bindMethods=true,wrapConstructed=value=>value){
 // An empty forwarding target avoids invariants on read-only native export properties.
 const target=typeof original==='function'?function(...args){return Reflect.apply(original,this,args);}:Object.create(Object.getPrototypeOf(original));
 return new Proxy(target,{construct(_target,args){return wrapConstructed(Reflect.construct(original,args,original));},get(_target,key){
  if(Object.prototype.hasOwnProperty.call(overrides,key))return overrides[key];
  const value=Reflect.get(original,key,original);
  return bindMethods&&typeof value==='function'?value.bind(original):value;
 }});
}
function observeNativeExports(nativeApi,emit,getGroups,onSession=()=>{}){
 const sessions=new WeakMap(),services=new WeakMap(),listeners=new WeakMap();
 const safeEmit=event=>{try{emit(event);}catch{/* Receiving must never interrupt QQ. */}};
 const wrapListener=listener=>{
  if(!listener||!['object','function'].includes(typeof listener))return listener;
  if(listeners.has(listener))return listeners.get(listener);
  const overrides={};
  for(const name of ['onRecvMsg','onRecvOnlineFileMsg']){
   const original=listener[name];
   if(typeof original!=='function')continue;
   overrides[name]=function(...args){
    try{for(const e of extractMessages(args[0],getGroups()))safeEmit(e);}catch{}
    return Reflect.apply(original,listener,args);
   };
  }
  const wrapped=forwardingProxy(listener,overrides);listeners.set(listener,wrapped);return wrapped;
 };
 const wrapService=service=>{
  if(!service||typeof service.addKernelMsgListener!=='function')return service;
  if(services.has(service))return services.get(service);
  const wrapped=forwardingProxy(service,{addKernelMsgListener:function(listener,...args){
   const result=Reflect.apply(service.addKernelMsgListener,service,[wrapListener(listener),...args]);
   safeEmit({type:'status',state:'listening'});return result;
  }});services.set(service,wrapped);return wrapped;
 };
 const wrapSession=session=>{
  if(!session||typeof session.getMsgService!=='function')return session;
  try{onSession(session);}catch{}
  if(sessions.has(session))return sessions.get(session);
  const wrapped=forwardingProxy(session,{getMsgService:function(...args){return wrapService(Reflect.apply(session.getMsgService,session,args));}});
  sessions.set(session,wrapped);return wrapped;
 };
 const factory=nativeApi?.NodeIQQNTWrapperSession;
 const acquisition=factory?['getNTWrapperSession','get','create'].filter(name=>typeof factory[name]==='function'):[];
 if(!acquisition.length){safeEmit({type:'status',state:'unsupported'});return nativeApi;}
 const methods={};
 for(const name of acquisition)methods[name]=function(...args){return wrapSession(Reflect.apply(factory[name],factory,args));};
 const wrappedFactory=forwardingProxy(factory,methods,true,wrapSession);
 // Native exports contain CLASS constructors. Binding them erases their static
 // .get() methods and prevents QQ's own login/engine startup.
 return forwardingProxy(nativeApi,{NodeIQQNTWrapperSession:wrappedFactory},false);
}
const TRANSPORT_PROTOCOL='qq-activity-http-v2';
function validBridge(config){return config?.host==='127.0.0.1'&&Number.isInteger(config.port)&&config.port>0&&config.port<65536&&typeof config.token==='string'&&config.token.length>=32;}
function createTransport(getConfig,options={}){
 const queue=[],seen=new Map();
 const maxQueue=options.maxQueue||200,maxSeen=options.maxSeen||4000,retryBase=options.retryBase||250;
 let heartbeat=null,active=null,timer=null,closed=false,messageRun=0;
 const stats={sent:0,failed:0,dropped:0,deduplicated:0};
 const forget=item=>{if(item.key&&seen.get(item.key)===item.digest)seen.delete(item.key);};
 function schedule(delay=0){if(closed||timer||active)return;timer=setTimeout(()=>{timer=null;pump();},delay);timer.unref?.();}
 function pump(){
  if(closed||active||(!queue.length&&!heartbeat))return;
  let config;try{config=getConfig();}catch{}
  if(!validBridge(config)){schedule(1000);return;}
  const item=heartbeat&&(!queue.length||messageRun>=10)?heartbeat:queue[0];
  if(item===heartbeat)heartbeat=null;else queue.shift();
  messageRun=item.event.type==='heartbeat'?0:messageRun+1;
  let body;try{body=Buffer.from(JSON.stringify(item.event));}catch{forget(item);stats.dropped++;schedule();return;}
  active={item,request:null};let settled=false;
  function settle(success){
   if(settled)return;settled=true;active=null;
   if(closed)return;
   if(success){stats.sent++;schedule();return;}
   stats.failed++;item.attempts++;
   if(item.attempts<5){
    if(item.event.type==='heartbeat'){if(!heartbeat)heartbeat=item;}
    else{queue.unshift(item);if(queue.length>maxQueue){forget(queue.pop());stats.dropped++;}}
   }else{forget(item);stats.dropped++;}
   schedule(Math.min(5000,retryBase*2**Math.min(item.attempts-1,5)));
  }
  try{
   const request=http.request({host:'127.0.0.1',port:config.port,path:'/events',method:'POST',headers:{'Content-Type':'application/json','Content-Length':body.length,'X-Bridge-Token':config.token},timeout:2500},response=>{
    response.on('end',()=>settle(response.statusCode>=200&&response.statusCode<300));
    response.on('error',()=>settle(false));response.on('aborted',()=>settle(false));response.resume();
   });
   active.request=request;request.on('error',()=>settle(false));request.on('timeout',()=>request.destroy(Error('bridge_timeout')));request.end(body);
  }catch{settle(false);}
 }
 function emit(event){
  if(closed||!event||typeof event!=='object')return false;
  const item={event:{...event},attempts:0};
  if(event.type==='message'){
   const config=getConfig();
   if(!(config.groups||[]).map(String).includes(String(event.group_id)))return false;
   item.key=JSON.stringify([String(event.group_id),String(event.message_id)]);
   item.digest=crypto.createHash('sha256').update(JSON.stringify([event.text,!!event.image_only])).digest('hex');
   if(seen.get(item.key)===item.digest){stats.deduplicated++;return false;}
   seen.delete(item.key);seen.set(item.key,item.digest);
   while(seen.size>maxSeen)seen.delete(seen.keys().next().value);
  }
  if(event.type==='heartbeat')heartbeat=item;
  else{if(queue.length>=maxQueue){forget(queue.shift());stats.dropped++;}queue.push(item);}
  schedule();return true;
 }
 return {protocol:TRANSPORT_PROTOCOL,emit,stats,close(){closed=true;if(timer)clearTimeout(timer);active?.request?.destroy();queue.length=0;heartbeat=null;seen.clear();},pending:()=>queue.length+(heartbeat?1:0)+(active?1:0)};
}
function installRuntimeTransport(runtime){
 if(runtime.transportProtocol===TRANSPORT_PROTOCOL&&runtime.transport)return;
 let config;try{config=runtime.getConfig();}catch{return;}
 if(!validBridge(config))return;
 const transport=createTransport(()=>runtime.getConfig());
 runtime.transport=transport;runtime.transportProtocol=TRANSPORT_PROTOCOL;
 runtime.callbackCount=runtime.callbackCount||0;
 runtime.emit=event=>{try{return transport.emit(event);}catch{return false;}};
 runtime.onRecords=(records,delivery='live')=>{
  try{for(const event of (runtime.extract||extractMessages)(records,runtime.getConfig().groups||[])){
   if(delivery!=='history')runtime.callbackCount++;
   runtime.emit({...event,delivery});
  }}catch{/* Never throw into a native QQ callback. */}
 };
 runtime.lastTransportHeartbeat=0;
 process.once('exit',()=>transport.close());
}
function reconcileRuntime(runtime){
 // Also runs in the original start() closure after development_reload replaces
 // this export. Existing listeners dispatch through runtime.onRecords, so the
 // transport can be upgraded without replacing any native listener/session.
 installRuntimeTransport(runtime);
 if(runtime.transportProtocol===TRANSPORT_PROTOCOL&&(!runtime.lastTransportHeartbeat||Date.now()-runtime.lastTransportHeartbeat>=5000)){
  runtime.lastTransportHeartbeat=Date.now();
  runtime.emit({type:'heartbeat',state:runtime.listener&&runtime.session&&runtime.service?'listening':runtime.nativeApi?'native_loaded':'waiting_native',callback_count:runtime.callbackCount||0,history_state:runtime.historyState,history_count:runtime.historyCount||0,time:Date.now()/1000});
 }
 if(!runtime.nativeApi)return;
 if(runtime.diagnosticVersion!==5){runtime.diagnosticVersion=5;runtime.diagnostic=false;runtime.lookupLogged=false;runtime.sessionChecks=0;}
 const factory=runtime.nativeApi.NodeIQQNTWrapperSession;
 if(!runtime.diagnostic){
  runtime.diagnostic=true;
  const shape=value=>({type:typeof value,own:value?Object.getOwnPropertyNames(value).slice(0,80):[],prototype:value?Object.getOwnPropertyNames(Object.getPrototypeOf(value)||{}).slice(0,120):[],promise:typeof value?.then});
  try{runtime.debug?.({existingSession:shape(runtime.session),msgNamespace:shape(runtime.nativeApi.NodeIKernelMsgService)});}catch{}
  try{runtime.debug?.({globalNames:Object.getOwnPropertyNames(globalThis).filter(name=>/session|launcher|qq|kernel|native/i.test(name)),launcherNames:globalThis.launcher?Object.getOwnPropertyNames(globalThis.launcher):[],engine:shape(runtime.nativeApi.NodeIQQNTWrapperEngine),startup:shape(runtime.nativeApi.NodeIQQNTStartupSessionWrapper)});}catch{}
  try{const engine=runtime.nativeApi.NodeIQQNTWrapperEngine?.get?.();runtime.debug?.({engineInstance:shape(engine),launcherConfigNames:globalThis.launcher?.configJson?Object.getOwnPropertyNames(globalThis.launcher.configJson):[]});}catch(error){runtime.debug?.({engineReadError:error.name});}
  try{const login=runtime.nativeApi.NodeIKernelLoginService?.get?.();runtime.debug?.({processPid:process.pid,processType:process.type,loginService:shape(login)});}catch{}
 }
 if(!runtime.session&&runtime.sessionChecks++<30){
  // These lookups retrieve the existing normal QQ session; no create/init/login calls.
  for(const name of ['getNTWrapperSession','get']){
   if(typeof factory?.[name]!=='function')continue;
   try{const candidate=Reflect.apply(factory[name],factory,name==='getNTWrapperSession'?['nt_1']:[]);if(!runtime.lookupLogged){runtime.lookupLogged=true;runtime.debug?.({lookup:name,resultType:typeof candidate,own:candidate?Object.getOwnPropertyNames(candidate).slice(0,80):[],prototype:candidate?Object.getOwnPropertyNames(Object.getPrototypeOf(candidate)||{}).slice(0,120):[]});}if(candidate&&typeof candidate.getMsgService==='function'){runtime.session=candidate;break;}}catch(error){if(!runtime.lookupLogged){runtime.lookupLogged=true;runtime.debug?.({lookup:name,errorType:error.name});}}
  }
 }
 if(!runtime.session)return;
 if(!runtime.service){try{runtime.service=runtime.session.getMsgService();runtime.debug?.({serviceType:typeof runtime.service,serviceOwn:runtime.service?Object.getOwnPropertyNames(runtime.service).slice(0,80):[],servicePrototype:runtime.service?Object.getOwnPropertyNames(Object.getPrototypeOf(runtime.service)||{}).slice(0,120):[]});}catch(error){runtime.debug?.({serviceError:error.name});return;}}
 const service=runtime.service;
 if(!runtime.listener&&typeof service?.addKernelMsgListener==='function'){
  const listener=new Proxy({onRecvMsg:records=>runtime.onRecords(records),onRecvOnlineFileMsg:records=>runtime.onRecords(records)},{get(target,key){return Reflect.get(target,key)||(()=>{});}});
  try{service.addKernelMsgListener(listener);runtime.listener=listener;runtime.emit({type:'status',state:'listening'});}catch{return;}
 }
 const config=runtime.getConfig();
 const seconds=config.poll_seconds?Math.max(10,Math.min(300,Number(config.poll_seconds)||15)):0;
 const requestKey=String(config.history_request||'')+(seconds?':'+Math.floor(Date.now()/(seconds*1000)):'');
 if(requestKey&&requestKey!==runtime.historyRequest&&runtime.historyState!=='running'&&runtime.listener&&typeof service?.getAioFirstViewLatestMsgs==='function'){
  runtime.historyRequest=requestKey;
  runtime.historyState='running';
  Promise.all((config.groups||[]).map(async group=>{
   const answer=await Promise.race([service.getAioFirstViewLatestMsgs({chatType:2,peerUid:String(group)},20),new Promise((_,reject)=>{const timer=setTimeout(()=>reject(Error('history_timeout')),8000);timer.unref?.();})]);
   if(answer?.result!==0||!Array.isArray(answer.msgList))throw Error('history_failed');
   runtime.onRecords(answer.msgList,'history');runtime.historyCount+=answer.msgList.length;
  })).then(()=>{runtime.historyState='complete';runtime.debug?.({history:'complete',count:runtime.historyCount});}).catch(()=>{runtime.historyState='failed';runtime.debug?.({history:'failed'});});
 }
}
function start(configPath){
 if(globalThis.__qqActivityReceiver)return;
 globalThis.__qqActivityReceiver=true;
 let config={},state='waiting_native';
 const runtime={nativeApi:null,session:null,service:null,listener:null,sessionChecks:0,historyState:'idle',historyCount:0,historyRequest:null,getConfig:()=>{refresh();return config;},emit:()=>{},onRecords:()=>{}};
 runtime.extract=extractMessages;
 runtime.debug=value=>{try{fs.appendFileSync(require('path').join(require('path').dirname(configPath),'receiver-shape.log'),JSON.stringify(value)+'\n');}catch{}};
 globalThis.__qqActivityRuntime=runtime;
 let reconcile=reconcileRuntime;
 let ownMtime=fs.statSync(__filename).mtimeMs;
 function refresh(){try{const c=JSON.parse(fs.readFileSync(configPath,'utf8'));if(validBridge(c))config=c;}catch{}}
 refresh();
 installRuntimeTransport(runtime);
 const original=process.dlopen;
 process.dlopen=function(module,filename,...args){
  const result=Reflect.apply(original,this,[module,filename,...args]);
  if(typeof filename==='string'&&/[\\/]wrapper\.node$/i.test(filename)){
   try{
    runtime.nativeApi=module.exports;
    const factory=module.exports?.NodeIQQNTWrapperSession;
    const shape={exports:Object.getOwnPropertyNames(module.exports).slice(0,250),factory:factory?Object.getOwnPropertyNames(factory).slice(0,100):[],methods:Object.fromEntries(['getNTWrapperSession','get','create'].map(name=>[name,typeof factory?.[name]]))};
    try{fs.appendFileSync(require('path').join(require('path').dirname(configPath),'receiver-shape.log'),JSON.stringify(shape)+'\n');}catch{}
    state='native_loaded';
    // Keep QQ's exports completely unchanged. Once its own startup has created
    // the existing session, reconcileRuntime attaches a separate read listener.
    // This avoids altering native classes, constructors, reflection or callbacks.
   }catch{state='unsupported';}
   process.dlopen=original;
  }
  return result;
 };
 const tick=setInterval(()=>{
  refresh();
  if(config.development_reload){try{const mtime=fs.statSync(__filename).mtimeMs;if(mtime!==ownMtime){ownMtime=mtime;delete require.cache[__filename];const updated=require(__filename);runtime.extract=updated.extractMessages;reconcile=updated.reconcileRuntime;runtime.sessionChecks=0;}}catch{}}
  try{reconcile(runtime);}catch{}
 },1000);tick.unref();
 runtime.emit({type:'status',state});
 process.once('exit',()=>{clearInterval(tick);runtime.transport?.close();});
}
module.exports={extractMessages,observeNativeExports,reconcileRuntime,createTransport,start};
