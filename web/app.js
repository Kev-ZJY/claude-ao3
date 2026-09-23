(() => {
  'use strict';
  const video = document.querySelector('#terminal-video');
  const message = document.querySelector('#video-message');
  const videoSource = video.querySelector('source').src;
  let portablePlayback;
  let objectURL;
  function waitForMetadata() {
    if (video.readyState > 0) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const controller = new AbortController();
      const timer = setTimeout(() => { controller.abort(); reject(new Error('metadata timeout')); }, 12000);
      const finish = callback => { clearTimeout(timer); controller.abort(); callback(); };
      video.addEventListener('loadedmetadata', () => finish(resolve), {signal: controller.signal});
      video.addEventListener('error', () => finish(reject), {signal: controller.signal});
    });
  }
  async function preparePlayback() {
    await waitForMetadata();
    if (objectURL) return portablePlayback;
    if (video.seekable.length && video.seekable.end(video.seekable.length - 1) > 0) return;
    // Some static preview servers lack Range responses. A local Blob makes the
    // same small recording seekable there without requiring a special server.
    if (!portablePlayback) {
      portablePlayback = (async () => {
        const response = await fetch(videoSource);
        if (!response.ok) throw new Error('video fetch failed');
        const blob = await response.blob();
        const position = video.currentTime;
        const wasPlaying = !video.paused;
        objectURL = URL.createObjectURL(blob);
        video.src = objectURL;
        video.load();
        await waitForMetadata();
        if (position > 0) video.currentTime = position;
        // Native controls remain available if the browser requires another
        // user gesture after the source changes.
        if (wasPlaying) await video.play().catch(() => {});
      })().catch(error => { portablePlayback = undefined; throw error; });
    }
    return portablePlayback;
  }
  function prepareNativeControls() {
    if (!objectURL && /^https?:$/.test(location.protocol)) preparePlayback().catch(() => {});
  }
  video.addEventListener('loadedmetadata', prepareNativeControls);
  if (video.readyState > 0) prepareNativeControls();
  video.addEventListener('error', () => { message.textContent = '视频加载失败，请刷新后重试。'; });
  video.addEventListener('playing', () => { message.textContent = ''; });
  const copyButton = document.querySelector('#copy-install');
  const copyMessage = document.querySelector('#copy-message');
  copyButton.addEventListener('click', async () => {
    const text = document.querySelector('#install-code').textContent.trim();
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement('textarea');
        area.value = text;
        area.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.append(area);
        area.select();
        const copied = document.execCommand('copy');
        area.remove();
        copyButton.focus();
        if (!copied) throw new Error('copy unsupported');
      }
      copyMessage.textContent = '已复制，粘贴到终端即可安装。';
    } catch (_) {
      copyMessage.textContent = '请选中上方命令，手动复制。';
    }
  });
})();
