document.addEventListener('DOMContentLoaded', function() {
  const startBtn = document.getElementById('start-demo');
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

  const INITIAL_LAST_UPDATE = '00:00:00';

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

  startBtn.addEventListener('click', function() {
    odysseyStatus.textContent = 'DELEGATING';
    odysseyTask.textContent = 'Demo Task';
    odysseyStep.textContent = '1/3';

    achillesStatus.textContent = 'WORKING';
    achillesTask.textContent = 'Demo Task';
    achillesStep.textContent = '1/3';

    taskStatus.textContent = 'RUNNING';

    updateLastUpdate();
  });

  completeBtn.addEventListener('click', function() {
    odysseyStatus.textContent = 'COMPLETE';
    odysseyTask.textContent = 'Demo Task';
    odysseyStep.textContent = '3/3';

    achillesStatus.textContent = 'COMPLETE';
    achillesTask.textContent = 'Demo Task';
    achillesStep.textContent = '3/3';

    taskStatus.textContent = 'COMPLETE';

    updateLastUpdate();
  });

  resetBtn.addEventListener('click', function() {
    odysseyStatus.textContent = 'IDLE';
    odysseyTask.textContent = 'None';
    odysseyStep.textContent = '0/0';

    achillesStatus.textContent = 'IDLE';
    achillesTask.textContent = 'None';
    achillesStep.textContent = '0/0';

    taskStatus.textContent = 'RUNNING';

    odysseyLastUpdate.textContent = INITIAL_LAST_UPDATE;
    achillesLastUpdate.textContent = INITIAL_LAST_UPDATE;
  });
});
