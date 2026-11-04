(function(){
  const root=document.documentElement;
  const lang=(root.lang||'en-CA').toLowerCase().startsWith('fr')?'fr':'en';
  const copyText=lang==='fr'?'Copié':'Copied';
  const selectText=lang==='fr'?'Sélectionner le texte':'Select text';
  const themeLabels=lang==='fr'?{light:'Passer au mode clair',dark:'Passer au mode sombre'}:{light:'Switch to light mode',dark:'Switch to dark mode'};
  const storedTheme=localStorage.getItem('wai-theme');
  const storedSize=localStorage.getItem('wai-font-size');
  if(storedTheme==='dark'||storedTheme==='light')root.dataset.theme=storedTheme;
  if(storedSize==='small'||storedSize==='medium'||storedSize==='large')root.dataset.fontSize=storedSize;
  const sizeSelect=document.getElementById('fontSizeSelect');
  if(sizeSelect){sizeSelect.value=root.dataset.fontSize||'medium';sizeSelect.addEventListener('change',()=>{root.dataset.fontSize=sizeSelect.value;localStorage.setItem('wai-font-size',sizeSelect.value);});}
  const themeButton=document.getElementById('themeToggle');
  if(themeButton){
    const syncThemeLabel=()=>{const next=root.dataset.theme==='dark'?'light':'dark';themeButton.setAttribute('aria-label',themeLabels[next]);themeButton.setAttribute('aria-pressed',String(root.dataset.theme==='dark'));};
    syncThemeLabel();
    themeButton.addEventListener('click',()=>{const next=root.dataset.theme==='dark'?'light':'dark';root.dataset.theme=next;localStorage.setItem('wai-theme',next);syncThemeLabel();});
  }
  document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{
    try{await navigator.clipboard.writeText(button.dataset.copy);const old=button.textContent;button.textContent=copyText;button.setAttribute('aria-label',copyText);setTimeout(()=>{button.textContent=old;},1800);}
    catch(e){button.textContent=selectText;}
  }));
  document.querySelectorAll('input[type=file]').forEach(input=>input.addEventListener('change',()=>{const label=input.closest('.file-drop');if(label&&input.files&&input.files[0]){const span=label.querySelector('span');if(span)span.textContent=input.files[0].name;}}));
})();
