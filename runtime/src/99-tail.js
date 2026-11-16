root.LivingPicture = { mount: LP.mount, unmount: LP.unmount, autoMount: LP.autoMount, instances: LP.instances, presets: LP.PRESETS, lib: LP, version: '0.2.1-candidate' };
if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { LP.autoMount(); });
  else LP.autoMount();
}
})(typeof window !== 'undefined' ? window : this);
