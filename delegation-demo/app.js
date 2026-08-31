document.addEventListener('DOMContentLoaded', function() {
  const startBtn = document.getElementById('start-demo');
  const pauseBtn = document.getElementById('pause-task');
  const cancelBtn = document.getElementById('cancel-task');
  const completeBtn = document.getElementById('complete-task');
  const resetBtn = document.getElementById('reset');

  const odysseyStatus = document.getElementById('odyssey-status');
  const odysseyTask = document.getElementById('odyssey-task');
  const odysseyStep = document.getElementById('odyssey-step');
  const odysseyLastUpdate = document.getElementById('odyssey-last-update');

  const achillesStatus = document.getElementById('achilles-status');
  const achillesTask = document.getElementById('achilles-task');
  const achillesStep = document.getElementById('achilles-step');
  const achillesLastUpdate = document.getElementById('achilles-last-update');

  const taskStatus = document.getElementById('task-status');
  const lastGatewayRun = document.getElementById('last-gateway-run');
  const recentEventsList = document.getElementById('recent-events-list');

  const progressBar = document.getElementById('progress-bar');
  const progressText = document.getElementById('progress-text');
  const etaText = document.getElementById('eta-text');

  const speedNormalBtn = document.getElementById('speed-normal');
  const speedFastBtn = document.getElementById('speed-fast');
  const speedSlowBtn = document.getElementById('speed-slow');

  const INITIAL_LAST_UPDATE = '00:00:00';
  let isPaused = false;
  let isRunning = false;
  let isCancelled = false;
  let progress = 0;
  let progressInterval = null;
  const recentEvents = [];

  const SPEEDS = {
    NORMAL: 100,
    FAST: 50,
    SLOW: 200
  };
  let currentSpeed = 'NORMAL';

  function formatLocalTime(date) {
    const h = String(date.getHours()).padStart(2, '0');
    const m = String(date.getMinutes()).padStart(2, '0');
    const s = String(date.getSeconds()).padStart(2, '0');
    return h + ':' + m + ':' + s;
  }

  function updateLastUpdate() {
    const now = formatLocalTime(new Date());
    odysseyLastUpdate.textContent = now;
    achillesLastUpdate.textContent = now;
  }

  function addEvent(type) {
    const now = formatLocalTime(new Date());
    recentEvents.unshift({ type: type, time: now });
    if (recentEvents.length > 5) {
      recentEvents.pop();
    }
    renderEvents();
  }

  function renderEvents() {
    recentEventsList.innerHTML = '';
    recentEvents.forEach(function(event) {
      const li = document.createElement('li');
      li.textContent = event.type + ' - ' + event.time;
      recentEventsList.appendChild(li);
    });
  }

  function updateProgressDisplay() {
    progressBar.style.width = progress + '%';
    progressText.textContent = progress + '%';
    updateETA();
  }

  function updateETA() {
    if (progress >= 100) {
      etaText.textContent = 'Complete';
    } else if (isCancelled) {
      etaText.textContent = 'Cancelled';
    } else if (isPaused) {
      etaText.textContent = 'Paused';
    } else if (!isRunning) {
      etaText.textContent = '--';
    } else {
      const remaining = 100 - progress;
      const msPerStep = SPEEDS[currentSpeed];
      const totalMs = remaining * msPerStep;
      const seconds = Math.ceil(totalMs / 1000);
      etaText.textContent = seconds + 's';
    }
  }

  function updateSpeedButtons() {
    speedNormalBtn.classList.toggle('active', currentSpeed === 'NORMAL');
    speedFastBtn.classList.toggle('active', currentSpeed === 'FAST');
    speedSlowBtn.classList.toggle('active', currentSpeed === 'SLOW');
  }

  function setSpeed(speed) {
    if (currentSpeed === speed) return;
    currentSpeed = speed;
    updateSpeedButtons();
    if (progressInterval) {
      clearInterval(progressInterval);
      progressInterval = null;
    }
    if (!isPaused && !isCancelled && progress < 100 && isRunning) {
      startProgress();
    }
    updateETA();
    addEvent('SPEED_' + speed);
  }

  function startProgress() {
    if (progressInterval) {
      clearInterval(progressInterval);
    }
    const intervalMs = SPEEDS[currentSpeed];
    progressInterval = setInterval(function() {
      if (!isPaused && !isCancelled && progress < 100) {
        progress += 1;
        if (progress > 100) progress = 100;
        updateProgressDisplay();
      }
    }, intervalMs);
  }

  function stopProgress() {
    if (progressInterval) {
      clearInterval(progressInterval);
      progressInterval = null;
    }
  }

  function updateCancelButton() {
    cancelBtn.disabled = !(isRunning && !isCancelled);
  }

  startBtn.addEventListener('click', function() {
    odysseyStatus.textContent = 'DELEGATING';
    odysseyTask.textContent = 'Authorized Task';
    odysseyStep.textContent = '1/3';

    achillesStatus.textContent = 'WORKING';
    achillesTask.textContent = 'Authorized Task';
    achillesStep.textContent = '1/3';

    taskStatus.textContent = 'RUNNING';
    lastGatewayRun.textContent = 'RUNNING';

    isPaused = false;
    isRunning = true;
    isCancelled = false;
    pauseBtn.disabled = false;
    pauseBtn.textContent = 'Pause Task';
    updateCancelButton();

    progress = 0;
    updateProgressDisplay();
    startProgress();

    updateLastUpdate();
    addEvent('START');
  });

  pauseBtn.addEventListener('click', function() {
    if (isPaused) {
      odysseyStatus.textContent = 'DELEGATING';
      achillesStatus.textContent = 'WORKING';
      taskStatus.textContent = 'RUNNING';
      pauseBtn.textContent = 'Pause Task';
      isPaused = false;
      if (progress < 100) {
        startProgress();
      }
      updateETA();
      addEvent('RESUME');
    } else {
      odysseyStatus.textContent = 'PAUSED';
      achillesStatus.textContent = 'PAUSED';
      taskStatus.textContent = 'PAUSED';
      pauseBtn.textContent = 'Resume Task';
      isPaused = true;
      updateETA();
      addEvent('PAUSE');
    }
    updateLastUpdate();
  });

  cancelBtn.addEventListener('click', function() {
    if (!isRunning || isCancelled) return;

    odysseyStatus.textContent = 'CANCELLED';
    achillesStatus.textContent = 'CANCELLED';
    taskStatus.textContent = 'CANCELLED';
    lastGatewayRun.textContent = 'CANCELLED';

    isCancelled = true;
    isPaused = false;
    pauseBtn.textContent = 'Pause Task';
    updateCancelButton();

    stopProgress();
    updateProgressDisplay();

    updateLastUpdate();
    addEvent('CANCEL');
  });

  completeBtn.addEventListener('click', function() {
    odysseyStatus.textContent = 'COMPLETE';
    odysseyTask.textContent = 'Authorized Task';
    odysseyStep.textContent = '3/3';

    achillesStatus.textContent = 'COMPLETE';
    achillesTask.textContent = 'Authorized Task';
    achillesStep.textContent = '3/3';

    taskStatus.textContent = 'COMPLETE';
    lastGatewayRun.textContent = 'VERIFIED';

    pauseBtn.disabled = true;
    pauseBtn.textContent = 'Pause Task';
    isPaused = false;
    isRunning = false;
    isCancelled = false;
    updateCancelButton();

    progress = 100;
    updateProgressDisplay();
    stopProgress();

    updateLastUpdate();
    addEvent('COMPLETE');
  });

  resetBtn.addEventListener('click', function() {
    odysseyStatus.textContent = 'IDLE';
    odysseyTask.textContent = 'None';
    odysseyStep.textContent = '0/0';

    achillesStatus.textContent = 'IDLE';
    achillesTask.textContent = 'None';
    achillesStep.textContent = '0/0';

    taskStatus.textContent = 'IDLE';
    lastGatewayRun.textContent = 'NONE';

    odysseyLastUpdate.textContent = INITIAL_LAST_UPDATE;
    achillesLastUpdate.textContent = INITIAL_LAST_UPDATE;

    pauseBtn.disabled = true;
    pauseBtn.textContent = 'Pause Task';
    isPaused = false;
    isRunning = false;
    isCancelled = false;
    updateCancelButton();

    progress = 0;
    updateProgressDisplay();
    stopProgress();

    currentSpeed = 'NORMAL';
    updateSpeedButtons();

    addEvent('RESET');
  });

  speedNormalBtn.addEventListener('click', function() {
    setSpeed('NORMAL');
  });

  speedFastBtn.addEventListener('click', function() {
    setSpeed('FAST');
  });

  speedSlowBtn.addEventListener('click', function() {
    setSpeed('SLOW');
  });
});