/* فرم‌های عمومی: پیش‌ثبت‌نام، پیگیری درخواست، ورود با کد پیامکی و درخواست‌های من */

(function () {
  'use strict';

  const api = window.SwApi;
  const H = window.SwHelpers;
  const { el, clear, showMessage, hideMessage, formatDate } = H;

  const STATUS_LABELS = {
    new: 'جدید',
    reviewing: 'در حال بررسی',
    approved: 'تأییدشده',
    rejected: 'ردشده',
    archived: 'بایگانی‌شده',
  };

  function statusBadge(status) {
    const badge = el('span', `badge badge--${status}`, STATUS_LABELS[status] || status);
    return badge;
  }

  function setFieldError(form, name, message) {
    const row = form.querySelector(`[data-field="${name}"]`);
    if (!row) return;
    const errorNode = row.querySelector('.form-row__error');
    if (errorNode) errorNode.textContent = message || '';
    row.classList.toggle('form-row--error', Boolean(message));
    const input = row.querySelector('input, select, textarea');
    if (input) input.setAttribute('aria-invalid', message ? 'true' : 'false');
  }

  function clearErrors(form) {
    form.querySelectorAll('.form-row').forEach((row) => row.classList.remove('form-row--error'));
    form.querySelectorAll('.form-row__error').forEach((node) => {
      node.textContent = '';
    });
  }

  /* ----------------------------- پیش‌ثبت‌نام ----------------------------- */

  async function initPreRegistration() {
    const form = document.getElementById('pre-registration-form');
    if (!form) return;

    const alertBox = document.getElementById('form-alert');
    const resultBox = document.getElementById('form-result');
    const submitButton = form.querySelector('button[type="submit"]');
    const gradeSelect = form.querySelector('[name="grade_title"]');
    const fieldSelect = form.querySelector('[name="field_title"]');
    const noticeNode = document.getElementById('pre-registration-notice');

    const data = await window.SwSite.loadSiteData();
    const settings = data.settings || {};

    if (noticeNode) {
      noticeNode.textContent =
        settings.pre_registration_notice ||
        'اطلاعات این فرم برای بررسی درخواست پیش‌ثبت‌نام استفاده می‌شود و برای دیگران نمایش داده نمی‌شود.';
    }

    if (!settings.preRegistrationOpen) {
      showMessage(alertBox, 'warning', 'فرم پیش‌ثبت‌نام در حال حاضر بسته است. برای اطلاع از زمان ثبت‌نام، اخبار سایت را دنبال کنید.');
      submitButton.disabled = true;
    }

    (data.grades || []).forEach((grade) => {
      const option = el('option', null, grade.title);
      option.value = grade.title;
      gradeSelect.append(option);
    });
    (data.studyFields || []).forEach((field) => {
      const option = el('option', null, field.title);
      option.value = field.title;
      fieldSelect.append(option);
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      hideMessage(alertBox);
      clearErrors(form);
      resultBox.hidden = true;

      const formData = new FormData(form);
      const payload = {
        student_name: String(formData.get('student_name') || '').trim(),
        mobile: String(formData.get('mobile') || '').trim(),
        grade_title: String(formData.get('grade_title') || ''),
        field_title: String(formData.get('field_title') || ''),
        parent_name: String(formData.get('parent_name') || '').trim(),
        note: String(formData.get('note') || '').trim(),
        agree: formData.get('agree') === 'on',
      };

      const localErrors = {};
      if (payload.student_name.length < 3) localErrors.student_name = 'نام دانش‌آموز را کامل وارد کنید.';
      if (!/^09\d{9}$/.test(payload.mobile.replace(/[۰-۹]/g, (d) => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(d)))))
        localErrors.mobile = 'شماره موبایل را به شکل ۰۹۱۲۳۴۵۶۷۸۹ وارد کنید.';
      if (!payload.grade_title) localErrors.grade_title = 'پایه تحصیلی را انتخاب کنید.';
      if (!payload.field_title) localErrors.field_title = 'رشته تحصیلی را انتخاب کنید.';
      if (payload.parent_name.length < 3) localErrors.parent_name = 'نام ولی را کامل وارد کنید.';
      if (!payload.agree) localErrors.agree = 'تأیید صحت اطلاعات الزامی است.';

      if (Object.keys(localErrors).length > 0) {
        Object.entries(localErrors).forEach(([name, message]) => setFieldError(form, name, message));
        showMessage(alertBox, 'error', 'لطفاً موارد مشخص‌شده را اصلاح کنید.');
        return;
      }

      submitButton.disabled = true;
      submitButton.textContent = 'در حال ارسال…';

      try {
        const response = await api.post('/api/pre-registrations', payload);
        clear(resultBox);
        const title = el('h3', null, 'درخواست شما ثبت شد');
        const text = el('p', null, 'کد پیگیری زیر را نزد خود نگه دارید. با این کد و شماره موبایل می‌توانید وضعیت درخواست را پیگیری کنید.');
        const code = el('p', 'mono', response.tracking_code);
        code.style.fontSize = '1.15rem';
        const link = el('a', 'btn btn--sm btn--turquoise', 'صفحه پیگیری درخواست');
        link.href = `/track?code=${encodeURIComponent(response.tracking_code)}`;
        resultBox.append(title, text, code, link);
        resultBox.className = 'tracking-result';
        resultBox.hidden = false;
        form.reset();
        showMessage(alertBox, 'success', response.message || 'درخواست ثبت شد.');
      } catch (error) {
        if (error.details) {
          Object.entries(error.details).forEach(([name, message]) => setFieldError(form, name, message));
        }
        showMessage(alertBox, 'error', error.message);
      } finally {
        submitButton.disabled = false;
        submitButton.textContent = 'ثبت درخواست پیش‌ثبت‌نام';
      }
    });
  }

  /* --------------------------- پیگیری درخواست --------------------------- */

  async function initTracking() {
    const form = document.getElementById('tracking-form');
    if (!form) return;
    const alertBox = document.getElementById('tracking-alert');
    const resultBox = document.getElementById('tracking-result');
    const submitButton = form.querySelector('button[type="submit"]');

    const params = new URLSearchParams(window.location.search);
    if (params.get('code')) form.querySelector('[name="tracking_code"]').value = params.get('code');

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      hideMessage(alertBox);
      resultBox.hidden = true;
      const formData = new FormData(form);
      submitButton.disabled = true;
      try {
        const response = await api.post('/api/pre-registrations/track', {
          tracking_code: String(formData.get('tracking_code') || '').trim(),
          mobile: String(formData.get('mobile') || '').trim(),
        });
        const item = response.request;
        clear(resultBox);
        const head = el('div', 'panel__head');
        head.append(el('h3', null, `درخواست ${item.student_name}`));
        head.append(statusBadge(item.status));
        resultBox.append(head);

        const list = el('dl', 'kv');
        const rows = [
          ['کد پیگیری', item.tracking_code],
          ['پایه تحصیلی', item.grade_title],
          ['رشته تحصیلی', item.field_title],
          ['زمان ثبت', formatDate(item.created_at, true)],
          ['آخرین بررسی', item.reviewed_at ? formatDate(item.reviewed_at, true) : 'در انتظار بررسی'],
        ];
        if (item.public_note) rows.push(['یادداشت مدرسه', item.public_note]);
        rows.forEach(([label, value]) => {
          const row = el('div', 'kv__row');
          row.append(el('dt', null, label));
          row.append(el('dd', null, value));
          list.append(row);
        });
        resultBox.append(list);
        resultBox.hidden = false;
      } catch (error) {
        showMessage(alertBox, 'error', error.message);
      } finally {
        submitButton.disabled = false;
      }
    });
  }

  /* ------------------- ورود با شماره موبایل و کد پیامکی ------------------ */

  function renderRequestList(container, items) {
    clear(container);
    if (!items || items.length === 0) {
      container.append(
        el('p', 'help-text', 'هنوز درخواست پیش‌ثبت‌نامی برای این شماره ثبت نشده است.')
      );
      return;
    }
    const table = el('table', 'data');
    const head = el('thead');
    const headRow = el('tr');
    ['دانش‌آموز', 'پایه', 'رشته', 'کد پیگیری', 'وضعیت', 'زمان ثبت'].forEach((label) =>
      headRow.append(el('th', null, label))
    );
    head.append(headRow);
    const body = el('tbody');
    items.forEach((item) => {
      const row = el('tr');
      row.append(el('td', null, item.student_name));
      row.append(el('td', null, item.grade_title));
      row.append(el('td', null, item.field_title));
      const codeCell = el('td', 'mono', item.tracking_code);
      row.append(codeCell);
      const statusCell = el('td');
      statusCell.append(statusBadge(item.status));
      row.append(statusCell);
      row.append(el('td', null, formatDate(item.created_at)));
      body.append(row);
    });
    table.append(head, body);
    const wrap = el('div', 'table-wrap');
    wrap.append(table);
    container.append(wrap);
  }

  async function initLogin() {
    const otpForm = document.getElementById('otp-form');
    if (!otpForm) return;

    const alertBox = document.getElementById('login-alert');
    const verifyBox = document.getElementById('verify-box');
    const sessionBox = document.getElementById('session-box');
    const requestsBox = document.getElementById('my-requests');
    const statusNote = document.getElementById('sms-status');

    let currentMobile = '';
    let session = null;

    async function refreshSession() {
      session = await api.get('/api/auth/session');
      if (session.principal === 'user') {
        otpForm.hidden = true;
        verifyBox.hidden = true;
        sessionBox.hidden = false;
        sessionBox.querySelector('[data-mobile]').textContent = H.toPersianDigits(session.user.mobile);
        const data = await api.get('/api/my/requests');
        renderRequestList(requestsBox, data.items);
      } else {
        sessionBox.hidden = true;
      }
    }

    try {
      const status = await api.get('/api/auth/sms-status');
      if (statusNote) {
        statusNote.textContent = status.enabled
          ? 'ورود با کد پیامکی فعال است.'
          : `ورود با کد پیامکی در حال حاضر فعال نیست. ${status.message}`;
      }
      if (!status.enabled) {
        // هیچ کدی ساخته نمی‌شود؛ پیام صادقانه نمایش داده می‌شود.
        if (statusNote) statusNote.className = 'alert alert--warning';
      }
    } catch (error) {
      if (statusNote) statusNote.textContent = 'وضعیت سرویس پیامک قابل دریافت نبود.';
    }

    document.getElementById('logout-button').addEventListener('click', async () => {
      await api.post('/api/auth/logout');
      window.location.reload();
    });

    otpForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      hideMessage(alertBox);
      const mobile = String(new FormData(otpForm).get('mobile') || '').trim();
      try {
        const response = await api.post('/api/auth/otp/request', { mobile });
        currentMobile = response.mobile;
        verifyBox.hidden = false;
        verifyBox.querySelector('[name="mobile_for_verify"]').value = currentMobile;
        showMessage(
          alertBox,
          response.simulated ? 'warning' : 'success',
          response.message || 'کد ورود ارسال شد.'
        );
      } catch (error) {
        showMessage(alertBox, 'error', error.message);
      }
    });

    verifyBox.addEventListener('submit', async (event) => {
      event.preventDefault();
      hideMessage(alertBox);
      const code = String(new FormData(verifyBox).get('code') || '').trim();
      try {
        await api.post('/api/auth/otp/verify', { mobile: currentMobile, code });
        showMessage(alertBox, 'success', 'ورود انجام شد.');
        await refreshSession();
      } catch (error) {
        showMessage(alertBox, 'error', error.message);
      }
    });

    try {
      await refreshSession();
    } catch (error) {
      showMessage(alertBox, 'error', error.message);
    }
  }

  async function initMyRequestsPage() {
    const container = document.getElementById('my-requests-page');
    if (!container) return;
    const alertBox = document.getElementById('my-requests-alert');
    try {
      const session = await api.get('/api/auth/session');
      if (session.principal !== 'user') {
        showMessage(alertBox, 'info', 'برای دیدن درخواست‌های خود ابتدا با شماره موبایل وارد شوید.');
        const link = el('a', 'btn btn--sm btn--navy', 'ورود با کد پیامکی');
        link.href = '/login';
        container.append(link);
        return;
      }
      const data = await api.get('/api/my/requests');
      renderRequestList(container, data.items);
    } catch (error) {
      showMessage(alertBox, 'error', error.message);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    initPreRegistration();
    initTracking();
    initLogin();
    initMyRequestsPage();
  });
})();
