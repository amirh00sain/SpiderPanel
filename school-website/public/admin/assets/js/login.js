/* ورود مدیران به پنل مدیریت */

(function () {
  'use strict';

  const api = window.SwApi;
  const H = window.SwHelpers;

  document.addEventListener('DOMContentLoaded', async () => {
    const form = document.getElementById('admin-login-form');
    const alertBox = document.getElementById('login-alert');
    const setupHint = document.getElementById('setup-hint');
    const submit = form.querySelector('button[type="submit"]');

    // اگر هیچ مدیری ساخته نشده باشد، راهنمای نصب نمایش داده می‌شود.
    try {
      const status = await api.get('/api/setup/status');
      if (status.needsSetup) {
        H.clear(setupHint);
        setupHint.append(document.createTextNode('هنوز حساب مدیری ساخته نشده است. '));
        const link = H.el('a', null, 'رفتن به صفحه نصب اولیه');
        link.href = '/admin/setup';
        setupHint.append(link);
        setupHint.hidden = false;
      }
    } catch (error) {
      /* در صورت خطا، فرم ورود بدون راهنما نمایش داده می‌شود */
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      H.hideMessage(alertBox);
      submit.disabled = true;
      submit.textContent = 'در حال ورود…';
      try {
        await api.post('/api/admin/login', {
          username: form.username.value.trim(),
          password: form.password.value,
        });
        window.location.replace('/admin');
      } catch (error) {
        H.showMessage(alertBox, 'error', error.message);
        submit.disabled = false;
        submit.textContent = 'ورود به پنل';
      }
    });
  });
})();
