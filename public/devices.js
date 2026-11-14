'use strict';
const HealthDevices=(()=>{
  let current=null,generation=0,expiryTimer,refreshing=false,refreshAgain=false,joining=false;
  const call=(action,extra={},signal)=>HealthLocal.request('/api/devices/pair',{action,...extra},{signal});
  const active=g=>g===generation&&$('#pair-modal').open;
  function error(message){$('#pair-error').textContent=message;}
  function reset(role){
    generation++;current={role};joining=false;clearTimeout(expiryTimer);
    $('#pair-title').textContent=role==='issuer'?'Connect a device':'Join shared data';
    $('#pair-info').textContent=role==='issuer'?'Scan this QR code with the other device. Compare the confirmation codes before approving. The code expires in five minutes and can be used only once.':'Compare this confirmation code with the device that showed the QR code. That device must approve you before you can join.';
    for(const id of ['pair-qr','pair-code','pair-warning','pair-export','pair-approve','pair-join'])$('#'+id).hidden=true;
    $('#pair-join').disabled=true;$('#pair-approve').disabled=false;$('#pair-cancel').disabled=false;
    $('#pair-status').textContent='Connecting…';error('');open('#pair-modal');
    return generation;
  }
  function show(result){
    current={...current,...result};$('#pair-code').textContent='Confirmation code: '+result.code;$('#pair-code').hidden=false;
    $('#pair-status').textContent=({issued:'Waiting for the other browser to scan.',requested:'Waiting for approval on the device showing the QR code.',approved:'Approved. The joining browser can now confirm the replacement.',activated:'Finishing the transfer…',completed:'Device connected.',rejected:'Pairing was canceled or rejected. Create a fresh QR code to try again.',expired:'This QR code has expired. Create a fresh one to try again.'})[result.state]||'Pairing unavailable.';
    $('#pair-approve').hidden=!(current.role==='issuer'&&result.state==='requested');
    if(current.role==='issuer'&&result.state==='requested')$('#pair-status').textContent=(result.label||'Another browser')+' requests access. Compare both codes, then approve or cancel.';
    $('#pair-join').hidden=current.role!=='joiner';$('#pair-join').disabled=result.state!=='approved'||joining;
    if(['completed','rejected','expired'].includes(result.state)){HealthLocal.watchPairing(null);$('#pair-qr').hidden=true;$('#pair-approve').hidden=true;}
    clearTimeout(expiryTimer);
    if(['issued','requested','approved'].includes(result.state))expiryTimer=setTimeout(()=>{if(current?.ticket===result.ticket)show({...result,state:'expired'});},Math.max(0,new Date(result.expires_at)-Date.now()));
  }
  async function create(){const g=reset('issuer');try{const result=await call('create');if(!active(g)){call('cancel',{ticket:result.ticket}).catch(()=>{});return;}$('#pair-qr').src=result.qr;$('#pair-qr').hidden=false;show(result);HealthLocal.watchPairing(result.ticket);}catch(e){if(active(g))error(e.message);}}
  async function requestJoin(secret){const g=reset('joiner');$('#pair-warning').hidden=false;$('#pair-export').hidden=false;try{const result=await call('request',{secret});if(!active(g)){call('cancel',{ticket:result.ticket}).catch(()=>{});return;}show(result);HealthLocal.watchPairing(result.ticket);}catch(e){if(active(g))error(e.message);}}
  async function refreshPair(){if(!current?.ticket||!$('#pair-modal').open||joining)return;const g=generation;try{const result=await call('status',{ticket:current.ticket});if(active(g))show(result);}catch(e){if(active(g))error(e.message);}}
  async function refresh(){
    if(refreshing){refreshAgain=true;return;}refreshing=true;
    const scope=dialogScope($('#settings-modal'));
    try{const data=await HealthLocal.read(),supported=data.meta.profile?.pairing;
      $('#connect-device').disabled=!supported||!navigator.onLine||!!data.meta.disconnected;
      $('#devices-help').textContent=supported?'Pair browsers to share data without an account. Each browser keeps its own session. Keep cookies and export backups regularly.':'Device pairing requires the hosted HTTPS service with the devices migration enabled.';
      if(!supported||!$('#settings-modal').open)return;
      const result=await HealthLocal.request('/api/devices/list',{},{signal:scope.signal});scope.check();
      $('#device-list').innerHTML=result.devices.map(d=>`<div class="device-row"><span class="grow">${esc(d.label)}${d.current?' · This device':''}</span>${d.current?'':`<button type="button" class="secondary" data-disconnect="${esc(d.id)}">Disconnect</button>`}</div>`).join('');$('#devices-feedback').textContent='';
    }catch(e){if(!scope.signal?.aborted&&$('#settings-modal').open)$('#devices-feedback').textContent=e.message;}finally{refreshing=false;if(refreshAgain){refreshAgain=false;refresh();}}
  }
  async function consumePairLink(){const match=/^#pair=([a-f0-9]{64})$/.exec(location.hash);if(!match)return;history.replaceState(null,'',location.pathname+location.search);await requestJoin(match[1]);}
  async function init(){
    $('#connect-device').addEventListener('click',create);
    $('#pair-cancel').addEventListener('click',()=>closeDialog($('#pair-modal')));
    $('#pair-approve').addEventListener('click',async()=>{const g=generation;$('#pair-approve').disabled=true;try{const result=await call('approve',{ticket:current.ticket});if(active(g))show(result);}catch(e){if(active(g))error(e.message);}});
    $('#pair-join').addEventListener('click',async()=>{if(joining||current?.state!=='approved')return;const g=generation,ticket=current.ticket;joining=true;$('#pair-join').disabled=true;$('#pair-status').textContent='Downloading and verifying shared data and photos. Your existing data is kept until the transfer is ready.';try{await HealthLocal.joinPairing(ticket,dialogScope($('#pair-modal')).signal);if(active(g)){current.state='completed';closeDialog($('#pair-modal'));notify('Device connected. Shared data is saved on this device.');}}catch(e){if(active(g)){error(e.message);joining=false;$('#pair-status').textContent='Transfer did not complete. Your data is kept locally. Close and scan a fresh QR code to retry.';}}});
    $('#pair-export').addEventListener('click',async()=>{const g=generation;try{const blob=await HealthBackup.exportZip();if(!active(g))return;const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='health-diet-before-pairing.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){if(active(g))error(e.message);}});
    $('#pair-modal').addEventListener('close',()=>{const old=current;current=null;generation++;clearTimeout(expiryTimer);error('');HealthLocal.watchPairing(null);if(old?.ticket&&!['completed','rejected','expired'].includes(old.state))HealthLocal.cancelPairing(old.ticket).catch(()=>{});});
    $('#device-list').addEventListener('click',async event=>{const button=event.target.closest('[data-disconnect]');if(!button)return;if(!confirm('Disconnect this browser? It will keep downloaded data locally, but lose access to shared data and syncing until approved again.'))return;const scope=dialogScope($('#settings-modal'));button.disabled=true;try{await HealthLocal.request('/api/devices/disconnect',{id:button.dataset.disconnect});scope.check();await refresh();}catch(e){if(!scope.signal?.aborted){$('#devices-feedback').textContent=e.message;button.disabled=false;}}});
    window.addEventListener('health-device-signal',()=>{refreshPair();if($('#settings-modal').open)refresh();});
    await refresh();
    window.addEventListener('hashchange',consumePairLink);
    await consumePairLink();
  }
  return {init,refresh};
})();
