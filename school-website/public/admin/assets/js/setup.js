/* نصب اولیه: ساخت امن اولین حساب مدیر */

(function () {
  'use strict';

  const api = window.SwApi;
  const H = window.SwHelpers;

  function setError(form, name, message) {
    const row = form.querySelector(`[name="${name}"]`);
    if (!row) return;
    const wrap = row.closest('.form-row');
    if (!wrap) return;
    wrap.classList.toggle('form-row--error', Boolean(message));
    const errorNode = wrap.querySelector('.form-row__error');
    if (errorNode) errorNode.textContent = message || '';
  }

  document.addEventListener('DOMContentLoaded', async () => {
    const form = document.getElementById('setup-form');
    const alertBox = document.getElementById('setup-alert');
    const submit = form.querySelector('button[type="submit"]');

    try {
      const status = await api.get('/api/setup/status');
      if (status.alreadyDone) {
        H.showMessage(alertBox, 'info', 'یک حساب مدیر وجود دارد؛ این صفحه غیرفعال است. لطفاً وارد شوید.');
        submit.disabled = true;
      } else if (!status.tokenRequired) {
        H.showMessage(
          alertBox,
          'warning',
          'توکن نصب در سرور تنظیم نشده است (ADMIN_SETUP_TOKEN). مقادیر را در .env تعیین و سرور را دوباره اجرا کنید.'
        );
      }
    } catch (error) {
      H.showMessage(alertBox, 'error', error.message);
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      H.hideMessage(alertBox);
      form.querySelectorAll('.form-row__error').forEach((node) => {
        node.textContent = '';
      });
      submit.disabled = true;
      try {
        const response = await api.post('/api/setup/admin', {
          setup_token: form.setup_token.value,
          username: form.username.value.trim(),
          full_name: form.full_name.value.trim(),
          password: form.password.value,
        });
        H.showMessage(alertBox, 'success', response.message);
        window.location.replace('/admin/login');
      } catch (error) {
        if (error.details) {
          Object.entries(error.details).forEach(([name, message]) => setError(form, name, message));
        }
        H.showMessage(alertBox, 'error', error.message);
        submit.disabled = false;
      }
    });
  });
})();
