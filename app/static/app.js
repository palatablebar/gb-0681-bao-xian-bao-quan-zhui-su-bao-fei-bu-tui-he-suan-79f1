'use strict';
const state={busy:false};
const feedback=document.getElementById('feedback');
async function submit(payload){if(state.busy)return;state.busy=true;feedback.textContent='核算中…';document.querySelectorAll('button').forEach(b=>b.disabled=true);try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const body=await response.json();if(!response.ok)throw new Error(body.message);feedback.textContent='批单已复算 / '+body.run_id;window.location.reload()}catch(error){feedback.textContent=error.message+'，请检查输入';}finally{state.busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}}
document.getElementById('run').addEventListener('click',()=>submit({sample:'dirty.csv'}));
document.getElementById('print').addEventListener('click',()=>window.print());
const form=document.getElementById('source');if(form)form.addEventListener('submit',event=>{event.preventDefault();const csv=document.getElementById('csv').value;submit(csv?{csv}:{sample:document.getElementById('sample').value})});
const review=document.getElementById('review');if(review)review.addEventListener('click',async()=>{if(review.disabled)return;try{const r=await fetch('/api/runs/'+review.dataset.run+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(review.dataset.version),decision:'通过'})});const d=await r.json();if(!r.ok)throw new Error(d.message);feedback.textContent='签审完成 / REV '+d.review_version;review.dataset.version=d.review_version;}catch(e){feedback.textContent=e.message;}});
const correctBtn=document.getElementById('correct');
if(correctBtn)correctBtn.addEventListener('click',async()=>{
  const rows=[...document.querySelectorAll('tr.corr-row')];const ledger=[];
  for(const tr of rows){
    const disposition=tr.querySelector('.disp').value;
    const basis=tr.querySelector('.basis').value.trim();
    if(!basis){feedback.textContent='每条处置都必须填写补正依据';return;}
    const afterText=tr.querySelector('.after').value.trim();let after={};
    if(afterText){try{after=JSON.parse(afterText);}catch(e){feedback.textContent='补正取值必须是合法 JSON：'+tr.dataset.target;return;}}
    if(disposition==='CORRECTED'&&Object.keys(after).length===0){feedback.textContent='补正重入必须提供补正后取值：'+tr.dataset.target;return;}
    ledger.push({target:tr.dataset.target.split('|'),disposition,after,basis});
  }
  if(!ledger.length){feedback.textContent='没有待处置的隔离记录';return;}
  correctBtn.disabled=true;feedback.textContent='补正重算中…';
  try{
    const r=await fetch('/api/runs/'+correctBtn.dataset.run+'/corrections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ledger})});
    const d=await r.json();if(!r.ok)throw new Error(d.message);
    feedback.textContent='后继批次已生成 / '+d.run_id;window.location.reload();
  }catch(e){feedback.textContent=e.message;correctBtn.disabled=false;}
});
