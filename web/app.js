(() => {
  'use strict';
  const video = document.querySelector('#terminal-video');
  const message = document.querySelector('#video-message');
  const sceneButtons = [...document.querySelectorAll('[data-scene]')];
  // Chapter starts observed in the native PTY recording.
  const scenes = { search: 3.2409, chapters: 20.5776, cover: 27.1917, resume: 35.0025 };
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let portablePlayback;
  let objectURL;
  let playRequest = 0;
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
        const response = await fetch('media/terminal-demo.mp4');
        if (!response.ok) throw new Error('video fetch failed');
        const blob = await response.blob();
        const position = video.currentTime;
        const wasPlaying = !video.paused;
        objectURL = URL.createObjectURL(blob);
        video.src = objectURL;
        video.load();
        await waitForMetadata();
        if (position > 0) video.currentTime = position;
        if (wasPlaying) await video.play();
      })().catch(error => { portablePlayback = undefined; throw error; });
    }
    return portablePlayback;
  }
  function timeLabel(seconds) {
    const total = Math.floor(seconds);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  }
  function updateScenes() {
    let current = '';
    for (const [name, seconds] of Object.entries(scenes)) {
      if (video.currentTime >= seconds) current = name;
    }
    sceneButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.scene === current)));
  }
  async function watch(seconds) {
    const request = ++playRequest;
    message.textContent = '';
    document.querySelector('#demo').scrollIntoView({behavior: reducedMotion.matches ? 'auto' : 'smooth', block: 'center'});
    try {
      await preparePlayback();
      if (request !== playRequest) return;
      if (typeof seconds === 'number') video.currentTime = Math.min(seconds, video.duration || seconds);
      await video.play();
      updateScenes();
    } catch (_) {
      message.textContent = '暂时无法自动播放，请点击视频播放键；也可下载录屏查看。';
    }
  }
  document.querySelector('[data-watch]').addEventListener('click', () => watch(0));
  sceneButtons.forEach(button => {
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => watch(scenes[button.dataset.scene]));
  });
  video.addEventListener('loadedmetadata', () => {
    if (Number.isFinite(video.duration)) document.querySelector('#video-duration').textContent = timeLabel(video.duration);
    if (!objectURL && /^https?:$/.test(location.protocol)) preparePlayback().catch(() => {});
  });
  video.addEventListener('timeupdate', updateScenes);
  video.addEventListener('error', () => { message.textContent = '录屏加载失败。请确认 media 文件夹已完整上传，或下载录屏查看。'; });
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
      copyMessage.textContent = '已复制。粘贴到 Mac 终端执行，安装完成后会启动阅读器。';
      copyButton.textContent = '已复制 ✓';
    } catch (_) {
      copyMessage.textContent = '请选中上方命令，手动复制。';
    }
  });
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  function selectTab(selected) {
    tabs.forEach(tab => {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
    });
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      selectTab(tabs[next]);
      tabs[next].focus();
    });
  });
})();
