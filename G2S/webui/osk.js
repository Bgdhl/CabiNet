/* osk.js — CabiNet's on-screen keyboard, for a hub with no physical keyboard.

   The hub's own touchscreen runs a kiosk browser (cage + chromium) that has
   no system keyboard, so any page that takes typing draws its own. Include
   this file and every text box on the page gets it: tap a box and the
   keyboard slides up from the bottom; tap away (or the ⌄ key) and it goes.

   When it turns itself on:
     ?osk=1 / ?osk=0     forced, and remembered in this browser
     ?kiosk=1 / ?kiosk=0 the hub's kiosk service opens /home?kiosk=1; carried
                         to the other pages for as long as the browser runs
     otherwise           a small screen (the 800x480 DSI) that is not a phone
                         or tablet — those bring their own keyboard
   A physical keyboard keeps working while this one is showing.

   Self-contained: no CDN (the slot VLAN is offline), no dependencies.
   Pages can read html.osk (keyboard available) and html.osk-open (showing;
   --osk-h is its height). data-osk-go="Sign in" on an input relabels enter.

   THE KIOSK'S LOOK lives here too, because this is the one file every page
   the kiosk can reach already loads. On the hub's own touchscreen
   (?kiosk=1, carried from page to page) there is no mouse and scrolling is
   a swipe, so:
     - the mouse pointer is hidden. It was never the user's: the Pi's HDMI
       ports register as pointer devices, so the kiosk compositor draws an
       arrow in the middle of the screen and leaves it there. If a real
       mouse is plugged in and moved, the pointer comes straight back.
     - scroll bars are hidden (a pale desktop bar down a dark touch page).
   Neither applies to a small browser window or a phone — only ?kiosk=1. */
(function(){
"use strict";
var KEY="cabinet_osk";

/* Is this the hub's own kiosk? Only on an explicit ?kiosk=1 — here, in the
   sign-in page's next=, or remembered from an earlier page in this tab. */
var KIOSK=false;
function kioskNow(k){
  if(k==="1") return true;
  if(k==="0") return false;
  try{return sessionStorage.getItem(KEY)==="1";}catch(e){return false;}
}
function decide(){
  var qs=new URLSearchParams(location.search), st=null, k, m;
  var o=qs.get("osk");
  if(o==="1"||o==="0"){
    try{localStorage.setItem(KEY,o);}catch(e){}
    k=qs.get("kiosk");
    if(k==="1"||k==="0"){ try{sessionStorage.setItem(KEY,k);}catch(e){} }
    KIOSK=kioskNow(k);
    return o==="1";
  }
  k=qs.get("kiosk");
  if(k===null){                       // the sign-in page carries it in next=
    m=/[?&]kiosk=([01])\b/.exec(qs.get("next")||"");
    if(m) k=m[1];
  }
  if(k==="1"||k==="0"){ try{sessionStorage.setItem(KEY,k);}catch(e){} }
  KIOSK=kioskNow(k);
  try{st=localStorage.getItem(KEY);}catch(e){}
  if(st==="1"||st==="0") return st==="1";
  if(k==="1") return true;
  if(k==="0") return false;
  try{st=sessionStorage.getItem(KEY);}catch(e){}
  if(st==="1"||st==="0") return st==="1";
  return window.innerWidth<=860&&window.innerHeight<=560&&
    !/Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent||"");
}
var ON=decide();
window.CabiOSK={enabled:ON, kiosk:KIOSK};
if(KIOSK) kioskLook();
if(!ON) return;
document.documentElement.classList.add("osk");

function kioskLook(){
  var root=document.documentElement, st=document.createElement("style");
  st.textContent=
   "html.kiosk-nocursor,html.kiosk-nocursor *{cursor:none!important}"+
   "html.kiosk-nobars,html.kiosk-nobars *{scrollbar-width:none!important}"+
   "html.kiosk-nobars::-webkit-scrollbar,"+
   "html.kiosk-nobars *::-webkit-scrollbar"+
   "{display:none!important;width:0!important;height:0!important}";
  (document.head||root).appendChild(st);
  root.classList.add("kiosk-nocursor","kiosk-nobars");
  /* A real mouse: bring the pointer back the moment it moves. A tap is not
     a mouse (pointerType "touch"), and the arrow the compositor parks on
     the screen reports no movement. */
  document.addEventListener("pointermove",function back(e){
    if(e.pointerType!=="mouse"||(!e.movementX&&!e.movementY)) return;
    root.classList.remove("kiosk-nocursor");
    document.removeEventListener("pointermove",back,true);
  },true);
}

var TEXT_TYPES={text:1,search:1,password:1,tel:1,url:1,email:1,number:1};
function ok(el){
  if(!el||el.disabled||el.readOnly) return false;
  if(el.tagName==="TEXTAREA") return true;
  return el.tagName==="INPUT"&&TEXT_TYPES[(el.type||"text").toLowerCase()]===1;
}

var root=null, cur=null, hidden=true;
var startVal="", origType=null, origIM=null;
var mode="abc", shift=0;              // shift: 0 off, 1 next letter, 2 locked
var padded=null, padPrev="";

var CSS=
 "#osk{position:fixed;left:0;right:0;bottom:0;z-index:1000;display:none;"+
 "background:#120c08;border-top:1px solid #3b2b1d;padding:6px 6px 8px;"+
 "user-select:none;-webkit-user-select:none;touch-action:manipulation;"+
 "box-shadow:0 -10px 30px rgba(0,0,0,.55)}"+
 "#osk .okr{display:flex;gap:5px;margin:0 auto 5px;max-width:860px}"+
 "#osk .okr:last-child{margin-bottom:0}"+
 "#osk .ok{flex:1 1 0;min-width:0;height:44px;height:clamp(44px,9vh,62px);"+
 "border-radius:7px;border:1px solid #3b2b1d;background:#281c13;"+
 "color:#f4ead7;font:600 19px -apple-system,'Segoe UI',Roboto,'DejaVu Sans',"+
 "Arial,sans-serif;padding:0;margin:0;cursor:pointer;appearance:none;"+
 "-webkit-appearance:none;-webkit-tap-highlight-color:transparent}"+
 "#osk .ok.dn{background:#d9a94f;color:#1a1108}"+
 "#osk .ok.w{flex:1.6 1 0;font-size:14px}"+
 "#osk .ok.sp{flex:5 1 0;font-size:13px;color:#b69d7c}"+
 "#osk .ok.act{border-color:#d9a94f;color:#f2d492}"+
 "#osk .ok.go{flex:2.2 1 0;font-size:14px;border-color:#d9a94f;color:#f2d492}"+
 "#osk.num .okr{max-width:420px}";

function esc(s){
  return String(s).replace(/&/g,"&amp;").replace(/"/g,"&quot;")
    .replace(/</g,"&lt;");
}
function ch(c){return '<button type="button" tabindex="-1" class="ok" data-c="'+
  esc(c)+'">'+esc(c)+'</button>';}
function fn(k,label,cls){return '<button type="button" tabindex="-1" class="ok '+
  (cls||"w")+'" data-k="'+k+'">'+esc(label)+'</button>';}
function goLabel(){
  return (cur&&cur.getAttribute("data-osk-go"))||
    (cur&&cur.tagName==="TEXTAREA"?"↵ line":"↵ enter");
}

function draw(){
  var h="", i, c, rows, s;
  if(mode==="num"){
    h+='<div class="okr">'+ch("1")+ch("2")+ch("3")+fn("bk","⌫")+'</div>'+
       '<div class="okr">'+ch("4")+ch("5")+ch("6")+fn("next","next ⇥")+'</div>'+
       '<div class="okr">'+ch("7")+ch("8")+ch("9")+fn("hide","⌄ hide")+'</div>'+
       '<div class="okr">'+ch("-")+ch("0")+ch(".")+
         fn("go",goLabel(),"w act")+'</div>';
  }else{
    rows=mode==="sym"
      ? ["1234567890","!@#$%^&*()","-_=+[]{};:","'\",<>/?\\|~"]
      : ["1234567890","qwertyuiop","asdfghjkl","zxcvbnm"];
    for(i=0;i<rows.length;i++){
      h+='<div class="okr">';
      if(i===3&&mode==="abc")
        h+=fn("shift",shift===2?"⇪":"⇧","w"+(shift?" act":""));
      for(c=0;c<rows[i].length;c++){
        s=rows[i].charAt(c);
        if(shift&&mode==="abc") s=s.toUpperCase();
        h+=ch(s);
      }
      if(i===3) h+=fn("bk","⌫");
      h+='</div>';
    }
    h+='<div class="okr">'+
       fn("sym",mode==="sym"?"abc":"#+=","w"+(mode==="sym"?" act":""))+
       fn("hide","⌄")+
       '<button type="button" tabindex="-1" class="ok sp" data-c=" ">space'+
       '</button>'+ch(".")+fn("next","next ⇥")+fn("go",goLabel(),"go")+
       '</div>';
  }
  root.className=mode==="num"?"num":"";
  root.innerHTML=h;
}

function build(){
  var st=document.createElement("style");
  st.textContent=CSS;
  document.head.appendChild(st);
  root=document.createElement("div");
  root.id="osk";
  root.setAttribute("aria-hidden","true");
  document.body.appendChild(root);
  root.addEventListener("pointerdown",press);
  /* Keep the text box focused: none of these may move focus to a key. */
  root.addEventListener("mousedown",function(e){e.preventDefault();});
  root.addEventListener("touchstart",function(e){e.preventDefault();},
    {passive:false});
  root.addEventListener("click",function(e){e.preventDefault();});
  ["pointerup","pointercancel","pointerleave"].forEach(function(n){
    root.addEventListener(n,release,true);
  });
}

/* ---- typing --------------------------------------------------------------- */
function sel(el){
  var a=null, b=null;
  try{a=el.selectionStart; b=el.selectionEnd;}catch(e){}
  if(typeof a!=="number"||typeof b!=="number") a=b=el.value.length;
  return [a,b];
}
function put(el,v,caret){
  el.value=v;
  try{el.setSelectionRange(caret,caret);}catch(e){}
  el.dispatchEvent(new Event("input",{bubbles:true}));
}
function insert(el,s){
  var v=el.value, r=sel(el), max=el.maxLength>0?el.maxLength:Infinity;
  if(v.length-(r[1]-r[0])+s.length>max) return;
  put(el,v.slice(0,r[0])+s+v.slice(r[1]),r[0]+s.length);
}
function backspace(el){
  var v=el.value, r=sel(el);
  if(r[0]!==r[1]) put(el,v.slice(0,r[0])+v.slice(r[1]),r[0]);
  else if(r[0]>0) put(el,v.slice(0,r[0]-1)+v.slice(r[0]),r[0]-1);
}
function enter(el){
  var ev=new KeyboardEvent("keydown",{key:"Enter",code:"Enter",keyCode:13,
    which:13,bubbles:true,cancelable:true});
  var go=el.dispatchEvent(ev);
  el.dispatchEvent(new KeyboardEvent("keyup",{key:"Enter",code:"Enter",
    keyCode:13,which:13,bubbles:true}));
  if(!go||!el.isConnected) return;
  if(el.tagName==="TEXTAREA"){insert(el,"\n"); return;}
  if(el.form){
    if(el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
  }
}
function visible(el){
  var r=el.getBoundingClientRect();
  return r.width>0&&r.height>0&&getComputedStyle(el).visibility!=="hidden";
}
function next(el){
  var scope=(el.closest&&el.closest("form,[role=dialog]"))||document;
  var all=scope.querySelectorAll("input,textarea"), list=[], i;
  for(i=0;i<all.length;i++) if(ok(all[i])&&visible(all[i])) list.push(all[i]);
  if(list.length<2) return;
  i=list.indexOf(el);
  list[(i+1)%list.length].focus();
}

var rep=null;
function stopRep(){ if(rep){clearTimeout(rep); rep=null;} }
function release(){
  stopRep();
  var d=root.querySelectorAll(".dn"), i;
  for(i=0;i<d.length;i++) d[i].classList.remove("dn");
}
function press(e){
  var b=e.target.closest?e.target.closest(".ok"):null;
  e.preventDefault();
  if(!b) return;
  if(!cur||!cur.isConnected){hide(); return;}
  if(document.activeElement!==cur) cur.focus();
  b.classList.add("dn");
  var k=b.getAttribute("data-k"), c=b.getAttribute("data-c"), el=cur;
  if(c!==null){
    insert(el,c);
    if(shift===1){shift=0; draw();}
  }else if(k==="bk"){
    backspace(el);
    stopRep();
    rep=setTimeout(function again(){
      if(cur!==el||!el.isConnected){rep=null; return;}
      backspace(el);
      rep=setTimeout(again,70);
    },420);
  }else if(k==="shift"){shift=(shift+1)%3; draw();}
  else if(k==="sym"){mode=mode==="sym"?"abc":"sym"; shift=0; draw();}
  else if(k==="next") next(el);
  else if(k==="hide") hide();
  else if(k==="go") enter(el);
}

/* ---- keeping the text box in view ------------------------------------------ */
function unpad(){
  if(padded){padded.style.paddingBottom=padPrev; padded=null;}
}
function reveal(){
  if(!cur||hidden) return;
  var H=root.offsetHeight, box=null, p, r, vis, d;
  document.documentElement.style.setProperty("--osk-h",H+"px");
  for(p=cur.parentElement;p&&p!==document.body;p=p.parentElement)
    if(getComputedStyle(p).position==="fixed"){box=p; break;}
  p=box||document.body;
  if(padded!==p){
    unpad();
    padded=p; padPrev=p.style.paddingBottom;
    p.style.paddingBottom=
      ((parseFloat(getComputedStyle(p).paddingBottom)||0)+H)+"px";
  }
  r=cur.getBoundingClientRect(); vis=window.innerHeight-H;
  if(r.bottom>vis-10||r.top<56){
    d=r.top+r.height/2-vis/2;
    if(box) box.scrollTop+=d; else window.scrollBy(0,d);
  }
}

/* ---- show / hide ----------------------------------------------------------- */
function show(){
  if(!root) build();
  hidden=false;
  draw();
  root.style.display="block";
  document.documentElement.classList.add("osk-open");
  reveal();
}
function hide(){
  hidden=true;
  stopRep();
  if(root) root.style.display="none";
  document.documentElement.classList.remove("osk-open");
  unpad();
}
function attach(el){
  if(cur===el){ if(hidden) show(); return; }
  detach();
  cur=el; startVal=el.value;
  origIM=el.getAttribute("inputmode");
  var t=(el.type||"").toLowerCase();
  var num=t==="number"||t==="tel"||origIM==="numeric"||origIM==="decimal";
  /* A number box refuses half-typed values ("5." or "-"), so it is a plain
     text box while the keyboard is on it and a number box again after. */
  origType=null;
  if(t==="number"){origType="number"; el.type="text";}
  el.setAttribute("inputmode","none");      // keeps any system keyboard down
  mode=num?"num":"abc"; shift=0;
  show();
}
function detach(){
  var el=cur;
  if(!el) return;
  cur=null;
  if(origIM===null) el.removeAttribute("inputmode");
  else el.setAttribute("inputmode",origIM);
  if(origType){
    var v=el.value.replace(/\.$/,"");
    if(v==="-") v="";
    if(v!==el.value) el.value=v;
    el.type=origType;
  }
  if(el.isConnected&&el.value!==startVal)
    el.dispatchEvent(new Event("change",{bubbles:true}));
}
function check(){
  var a=document.activeElement;
  if(cur&&a===cur) return;
  if(root&&root.contains(a)){ if(cur&&cur.isConnected) cur.focus(); return; }
  if(ok(a)){attach(a); return;}
  detach(); hide();
}

document.addEventListener("focusin",function(e){
  if(root&&root.contains(e.target)) return;
  if(ok(e.target)) attach(e.target);
},true);
document.addEventListener("focusout",function(){setTimeout(check,0);},true);
/* Tapping the box you are already in brings a hidden keyboard back. */
document.addEventListener("pointerdown",function(e){
  if(hidden&&cur&&e.target===cur) show();
},true);
window.addEventListener("resize",function(){ if(!hidden) reveal(); });
/* A page that redraws can remove the box without a blur. */
setInterval(function(){
  if(cur&&(!cur.isConnected||cur.disabled||cur.readOnly)){cur=null; hide();}
},500);
if(ok(document.activeElement)) attach(document.activeElement);
})();
