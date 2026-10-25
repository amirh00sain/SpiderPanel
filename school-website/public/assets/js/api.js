/* ابزارهای مشترک سمت مرورگر: ارتباط با API، توکن CSRF و کمکی‌های نمایشی */

(function () {
  'use strict';

  const state = { csrfToken: '' };

  async function ensureCsrfToken() {
    if (state.csrfToken) return state.csrfToken;
    try {
      const response = await fetch('/api/auth/session', {
        credentials: 'same-origin',
        headers: { accept: 'application/json' },
      });
      const data = await response.json().catch(() => ({}));
      if (data && data.csrfToken) {
        state.csrfToken = data.csrfToken;
        return state.csrfToken;
      }
    } catch (error) {
      /* در ادامه تلاش دوم انجام می‌شود */
    }
    try {
      const response = await fetch('/api/csrf', {
        credentials: 'same-origin',
        headers: { accept: 'application/json' },
      });
      const data = await response.json().catch(() => ({}));
      state.csrfToken = (data && data.csrfToken) || '';
    } catch (error) {
      state.csrfToken = '';
    }
    return state.csrfToken;
  }

  async function request(method, url, body, options) {
    const opts = options || {};
    const headers = { accept: 'application/json' };
    let payload;

    if (body instanceof FormData) {
      payload = body;
    } else if (body !== undefined) {
      headers['content-type'] = 'application/json';
      payload = JSON.stringify(body);
    }

    if (method !== 'GET') {
      headers['x-csrf-token'] = await ensureCsrfToken();
    }

    let response;
    try {
      response = await fetch(url, {
        method,
        headers,
        credentials: 'same-origin',
        body: payload,
      });
    } catch (error) {
      const networkError = new Error('ارتباط با سرور برقرار نشد. اتصال اینترنت خود را بررسی کنید.');
      networkError.network = true;
      throw networkError;
    }

    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (error) {
        data = { ok: false, error: 'پاسخ سرور قابل خواندن نبود.' };
      }
    }

    if (!response.ok || data.ok === false) {
      const apiError = new Error(data.error || 'درخواست انجام نشد.');
      apiError.status = response.status;
      apiError.details = data.details || null;
      throw apiError;
    }
    return data;
  }

  const api = {
    ensureCsrfToken,
    get: (url, options) => request('GET', url, undefined, options),
    post: (url, body, options) => request('POST', url, body, options),
    put: (url, body, options) => request('PUT', url, body, options),
    patch: (url, body, options) => request('PATCH', url, body, options),
    del: (url, options) => request('DELETE', url, undefined, options),
    upload: (url, file) => {
      const form = new FormData();
      form.append('file', file);
      return request('POST', url, form);
    },
  };

  const digits = ['۰', '۱', '۲', '۳', '۴', '۵', '۶', '۷', '۸', '۹'];

  function toPersianDigits(value) {
    return String(value === undefined || value === null ? '' : value).replace(
      /[0-9]/g,
      (digit) => digits[Number(digit)]
    );
  }

  function formatNumber(value) {
    const number = Number(value || 0);
    return toPersianDigits(number.toLocaleString('en-US'));
  }

  function formatDate(value, withTime) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    try {
      const options = withTime
        ? { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' }
        : { year: 'numeric', month: 'long', day: 'numeric' };
      return new Intl.DateTimeFormat('fa-IR', options).format(date);
    } catch (error) {
      return toPersianDigits(date.toISOString().slice(0, 10));
    }
  }

  function escapeHtml(value) {
    return String(value === undefined || value === null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function clear(node) {
    if (node) node.replaceChildren();
  }

  function setText(selector, value) {
    const node = typeof selector === 'string' ? document.querySelector(selector) : selector;
    if (node) node.textContent = value === undefined || value === null ? '' : String(value);
  }

  function emptyState(container, title, message) {
    clear(container);
    const box = el('div', 'empty-state');
    box.append(el('strong', null, title));
    if (message) box.append(el('p', null, message));
    container.append(box);
  }

  function showMessage(node, type, message) {
    if (!node) return;
    node.className = `alert alert--${type}`;
    node.textContent = message;
    node.hidden = false;
  }

  function hideMessage(node) {
    if (node) node.hidden = true;
  }

  function revealOnScroll() {
    const items = document.querySelectorAll('.reveal');
    if (!('IntersectionObserver' in window)) {
      items.forEach((item) => item.classList.add('is-visible'));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible');
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12 }
    );
    items.forEach((item) => observer.observe(item));
  }

  window.SwApi = api;
  window.SwHelpers = {
    toPersianDigits,
    formatNumber,
    formatDate,
    escapeHtml,
    el,
    clear,
    setText,
    emptyState,
    showMessage,
    hideMessage,
    revealOnScroll,
  };
})();
