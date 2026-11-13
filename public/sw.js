'use strict';
// Only public application files are cached here. Private records/images live
// in the origin's IndexedDB; API responses and cookies never enter this cache.
const CACHE='health-diet-shell-__SHELL_VERSION__';
const SHELL=['/','/index.html','/style.css','/local-store.js','/local-backup.js','/app.js'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('health-diet-shell-')&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);if(url.origin!==location.origin||event.request.method!=='GET')return;
  // The version is computed from all shell files by app.py. A new worker
  // installs a complete matching shell; normal reopens never wait on Render.
  if(SHELL.includes(url.pathname))event.respondWith(caches.open(CACHE).then(async cache=>(await cache.match(url.pathname))||fetch(event.request)));
});
