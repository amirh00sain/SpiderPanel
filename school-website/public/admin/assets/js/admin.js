/* پنل مدیریت: چیدمان، داشبورد، مدیریت محتوا، پیش‌ثبت‌نام‌ها و تنظیمات */

(function () {
  'use strict';

  const api = window.SwApi;
  const H = window.SwHelpers;
  const { el, clear, emptyState, formatDate, formatNumber, showMessage, hideMessage, toPersianDigits } = H;

  const NAV_GROUPS = [
    {
      title: 'مدیریت',
      items: [
        { page: 'index', label: 'داشبورد', icon: '📊' },
        { page: 'preregistrations', label: 'پیش‌ثبت‌نام‌ها', icon: '📝' },
        { page: 'news', label: 'اخبار', icon: '📰' },
        { page: 'announcements', label: 'اطلاعیه‌ها', icon: '📌' },
        { page: 'grades', label: 'پایه‌های تحصیلی', icon: '🎓' },
        { page: 'study-fields', label: 'رشته‌های تحصیلی', icon: '📚' },
        { page: 'staff', label: 'کادر مدرسه', icon: '👥' },
        { page: 'achievements', label: 'افتخارات', icon: '🏆' },
        { page: 'gallery', label: 'گالری تصاویر', icon: '🖼️' },
        { page: 'faqs', label: 'پرسش‌های متداول', icon: '❓' },
      ],
    },
    {
      title: 'سایت',
      items: [
        { page: 'school-info', label: 'اطلاعات مدرسه', icon: '🏫' },
        { page: 'settings', label: 'تنظیمات عمومی', icon: '⚙️' },
      ],
    },
    {
      title: 'سیستم',
      items: [
        { page: 'sms', label: 'تنظیمات پیامک', icon: '📱' },
        { page: 'users', label: 'کاربران', icon: '👤' },
        { page: 'admins', label: 'مدیران پنل', icon: '🔐', ownerOnly: true },
        { page: 'security', label: 'گزارش رویدادها', icon: '🛡️' },
        { page: 'password', label: 'تغییر رمز عبور', icon: '🔑' },
      ],
    },
  ];

  const PAGE_TITLES = {
    index: 'داشبورد',
    preregistrations: 'مدیریت پیش‌ثبت‌نام‌ها',
    news: 'مدیریت اخبار',
    announcements: 'مدیریت اطلاعیه‌ها',
    grades: 'مدیریت پایه‌های تحصیلی',
    'study-fields': 'مدیریت رشته‌های تحصیلی',
    staff: 'مدیریت کادر مدرسه',
    achievements: 'مدیریت افتخارات',
    gallery: 'مدیریت گالری تصاویر',
    faqs: 'مدیریت پرسش‌های متداول',
    'school-info': 'اطلاعات مدرسه',
    settings: 'تنظیمات عمومی سایت',
    sms: 'تنظیمات سرویس پیامک',
    users: 'کاربران سایت',
    admins: 'مدیران پنل',
    security: 'گزارش رویدادهای امنیتی',
    password: 'تغییر رمز عبور',
  };

  const ENTITY_PAGES = [
    'news',
    'announcements',
    'grades',
    'study-fields',
    'staff',
    'achievements',
    'gallery',
    'faqs',
    'admins',
  ];

  const session = { admin: null, csrfToken: '' };

  /* --------------------------- چیدمان پنل --------------------------- */

  function renderShell(activePage) {
    document.body.classList.add('admin');
    const root = document.getElementById('admin-shell');
    clear(root);

    const layout = el('div', 'admin-layout');

    const sidebar = el('aside', 'admin-sidebar');
    const brand = el('div', 'admin-sidebar__brand');
    const logo = el('div', 'brand__logo', '🏫');
    const brandText = el('div');
    brandText.append(el('strong', null, 'پنل مدیریت'));
    brandText.append(el('span', null, 'دبیرستان امام امیرالمؤمنین (ع)'));
    brand.append(logo, brandText);
    sidebar.append(brand);

    const nav = el('nav', 'admin-nav');
    nav.setAttribute('aria-label', 'فهرست پنل مدیریت');
    NAV_GROUPS.forEach((group) => {
      const items = group.items.filter((item) => !item.ownerOnly || session.admin.role === 'owner');
      if (items.length === 0) return;
      nav.append(el('div', 'admin-nav__group-title', group.title));
      items.forEach((item) => {
        const link = el('a');
        link.href = item.page === 'index' ? '/admin' : `/admin/${item.page}`;
        link.append(el('span', 'icon', item.icon));
        link.append(el('span', null, item.label));
        if (item.page === activePage) link.setAttribute('aria-current', 'page');
        nav.append(link);
      });
    });
    sidebar.append(nav);

    const logout = el('button', 'btn btn--ghost btn--block btn--sm', 'خروج از پنل');
    logout.type = 'button';
    logout.style.marginTop = 'auto';
    logout.addEventListener('click', async () => {
      await api.post('/api/auth/logout');
      window.location.href = '/admin/login';
    });
    sidebar.append(logout);

    const main = el('div', 'admin-main');
    const topbar = el('div', 'admin-topbar');
    const toggle = el('button', 'sidebar-toggle', '☰');
    toggle.type = 'button';
    toggle.setAttribute('aria-label', 'نمایش فهرست');
    toggle.addEventListener('click', () => document.body.classList.toggle('admin-nav-open'));
    const title = el('div', 'admin-topbar__title', PAGE_TITLES[activePage] || 'پنل مدیریت');
    const meta = el('div', 'admin-topbar__meta');
    meta.append(el('span', null, formatDate(new Date(), false)));
    meta.append(el('span', null, session.admin.full_name || session.admin.username));
    const siteLink = el('a', null, 'مشاهده سایت ↗');
    siteLink.href = '/';
    siteLink.target = '_blank';
    siteLink.rel = 'noopener noreferrer';
    meta.append(siteLink);
    topbar.append(toggle, title, meta);

    const content = el('div', 'admin-content');
    content.id = 'admin-page';
    main.append(topbar, content);
    layout.append(sidebar, main);
    root.append(layout);
    return content;
  }

  /* --------------------------- ابزار عمومی --------------------------- */

  function alertInto(container) {
    const box = el('div', 'alert');
    box.hidden = true;
    container.append(box);
    return box;
  }

  function askConfirm(message) {
    return new Promise((resolve) => {
      const drawer = el('div', 'drawer');
      const box = el('div', 'drawer__box');
      box.append(el('h3', null, 'تأیید عملیات'));
      box.append(el('p', null, message));
      const actions = el('div', 'form-actions');
      const yes = el('button', 'btn btn--danger', 'تأیید و ادامه');
      const no = el('button', 'btn btn--ghost', 'انصراف');
      yes.type = 'button';
      no.type = 'button';
      yes.addEventListener('click', () => {
        drawer.remove();
        resolve(true);
      });
      no.addEventListener('click', () => {
        drawer.remove();
        resolve(false);
      });
      actions.append(yes, no);
      box.append(actions);
      drawer.append(box);
      document.body.append(drawer);
    });
  }

  /* ---------------------------- داشبورد ---------------------------- */

  async function renderDashboard(container) {
    const panelLoading = el('div', 'panel');
    panelLoading.append(el('div', 'skeleton'));
    container.append(panelLoading);

    const data = await api.get('/api/admin/dashboard');
    clear(container);
    const stats = data.stats;

    const statGrid = el('div', 'stat-grid');
    const cards = [
      { label: 'بازدید سایت', value: stats.pageViewsTotal, icon: '👁️', tone: '' },
      { label: 'کل پیش‌ثبت‌نام‌ها', value: stats.preRegistrationsTotal, icon: '📝', tone: 'gold' },
      { label: 'درخواست‌های جدید', value: stats.preRegistrationsNew, icon: '🆕', tone: 'turquoise' },
      { label: 'اخبار منتشرشده', value: stats.newsPublished, icon: '📰', tone: '' },
      { label: 'تصاویر گالری', value: stats.galleryTotal, icon: '🖼️', tone: 'turquoise' },
      { label: 'اعضای کادر', value: stats.staffTotal, icon: '👥', tone: 'green' },
    ];
    cards.forEach((card) => {
      const box = el('div', `stat-card ${card.tone ? `stat-card--${card.tone}` : ''}`);
      box.append(el('div', 'stat-card__icon', card.icon));
      const body = el('div');
      body.append(el('strong', null, formatNumber(card.value)));
      body.append(el('span', null, card.label));
      box.append(body);
      statGrid.append(box);
    });
    container.append(statGrid);

    const chartPanel = el('section', 'panel');
    chartPanel.append(el('h2', null, 'روند ۷ روز گذشته'));
    const legend = el('div', 'help-text', 'میله‌های روشن: بازدید سایت — میله‌های تیره: پیش‌ثبت‌نام');
    chartPanel.append(legend);

    const maxValue = Math.max(1, ...data.charts.preRegistrations, ...data.charts.pageViews);
    const chart = el('div', 'chart');
    data.charts.days.forEach((day, index) => {
      const views = data.charts.pageViews[index];
      const prereg = data.charts.preRegistrations[index];
      const bar = el('div', 'chart__bar');
      bar.style.height = `${Math.max((views / maxValue) * 150, 4)}px`;
      bar.append(el('span', null, formatNumber(views)));
      const second = el('div', 'chart__bar');
      second.style.height = `${Math.max((prereg / maxValue) * 150, 4)}px`;
      second.style.background = 'linear-gradient(180deg, var(--gold-500), var(--gold-600))';
      second.style.width = '40%';
      const cell = el('div');
      cell.style.flex = '1';
      cell.style.display = 'flex';
      cell.style.alignItems = 'flex-end';
      cell.style.gap = '4px';
      cell.append(bar, second);
      chart.append(cell);
    });
    chartPanel.append(chart);

    const labels = el('div', 'chart__labels');
    data.charts.days.forEach((day) => labels.append(el('span', null, toPersianDigits(day.slice(5)))));
    chartPanel.append(labels);
    container.append(chartPanel);

    const recentPanel = el('section', 'panel');
    const head = el('div', 'panel__head');
    head.append(el('h2', null, 'آخرین درخواست‌های پیش‌ثبت‌نام'));
    const allLink = el('a', 'btn btn--sm btn--ghost', 'همه درخواست‌ها');
    allLink.href = '/admin/preregistrations';
    head.append(allLink);
    recentPanel.append(head);

    const wrap = el('div', 'table-wrap');
    const table = el('table', 'data');
    const thead = el('thead');
    const headRow = el('tr');
    ['دانش‌آموز', 'پایه', 'رشته', 'کد پیگیری', 'وضعیت', 'زمان ثبت'].forEach((label) =>
      headRow.append(el('th', null, label))
    );
    thead.append(headRow);
    const tbody = el('tbody');
    if (data.recentPreRegistrations.length === 0) {
      tbody.append(el('tr'));
      const cell = el('td', null, 'هنوز درخواستی ثبت نشده است.');
      cell.colSpan = 6;
      tbody.firstChild.append(cell);
    } else {
      data.recentPreRegistrations.forEach((item) => {
        const row = el('tr');
        row.append(el('td', null, item.student_name));
        row.append(el('td', null, item.grade_title));
        row.append(el('td', null, item.field_title));
        row.append(el('td', 'mono', item.tracking_code));
        const statusCell = el('td');
        statusCell.append(el('span', `badge badge--${item.status}`, STATUS_LABELS[item.status] || item.status));
        row.append(statusCell);
        row.append(el('td', null, formatDate(item.created_at)));
        tbody.append(row);
      });
    }
    table.append(thead, tbody);
    wrap.append(table);
    recentPanel.append(wrap);
    container.append(recentPanel);

    const notesPanel = el('section', 'panel');
    notesPanel.append(el('h2', null, 'وضعیت محتوا و سرویس‌ها'));
    const list = el('ul', 'footer-links');
    list.style.color = 'var(--gray-800)';

    if (data.content.missingFields.length > 0) {
      const li = el('li');
      li.append(el('span', 'badge badge--reviewing', 'نیاز به تکمیل'));
      li.append(
        document.createTextNode(
          ` اطلاعات مدرسه ناقص است: ${data.content.missingFields.join('، ')} — از بخش «اطلاعات مدرسه» تکمیل کنید.`
        )
      );
      list.append(li);
    } else {
      const li = el('li');
      li.append(el('span', 'badge badge--approved', 'تکمیل'));
      li.append(document.createTextNode(' اطلاعات پایه مدرسه تکمیل شده است.'));
      list.append(li);
    }

    const smsLi = el('li');
    smsLi.append(el('span', `badge badge--${data.sms.enabled ? 'approved' : 'rejected'}`, data.sms.enabled ? 'فعال' : 'غیرفعال'));
    smsLi.append(document.createTextNode(` پیامک: ${data.sms.message}`));
    list.append(smsLi);

    const newsLi = el('li');
    newsLi.append(
      document.createTextNode(
        `محتوای منتشرشده: ${formatNumber(stats.newsPublished)} خبر، ${formatNumber(stats.announcementsPublished)} اطلاعیه، ${formatNumber(stats.galleryTotal)} تصویر، ${formatNumber(stats.staffTotal)} عضو کادر.`
      )
    );
    list.append(newsLi);

    notesPanel.append(list);
    container.append(notesPanel);
  }

  const STATUS_LABELS = {
    new: 'جدید',
    reviewing: 'در حال بررسی',
    approved: 'تأییدشده',
    rejected: 'ردشده',
    archived: 'بایگانی‌شده',
  };

  /* --------------------------- CRUD محتوا --------------------------- */

  function fieldInput(field, value) {
    const wrapper = el('div', 'form-row');
    wrapper.dataset.field = field.name;
    const label = el('label');
    label.textContent = field.label + (field.required ? ' *' : '');
    label.htmlFor = `field-${field.name}`;
    wrapper.append(label);

    let input;
    if (field.type === 'textarea') {
      input = el('textarea');
      input.value = value === undefined || value === null ? '' : String(value);
    } else if (field.type === 'select') {
      input = el('select');
      (field.options || []).forEach((option) => {
        const node = el('option', null, option.label);
        node.value = option.value;
        input.append(node);
      });
      input.value = value === undefined || value === null ? (field.default || '') : String(value);
    } else if (field.type === 'boolean') {
      input = el('input');
      input.type = 'checkbox';
      input.checked = value === undefined ? Boolean(field.default) : Boolean(value) && value !== 0;
    } else if (field.type === 'datetime') {
      input = el('input');
      input.type = 'datetime-local';
      input.value = value || '';
    } else {
      input = el('input');
      input.type =
        field.type === 'number'
          ? 'number'
          : field.type === 'email'
            ? 'email'
            : field.type === 'password'
              ? 'password'
              : field.type === 'color'
                ? 'color'
                : field.type === 'tel'
                  ? 'tel'
                  : 'text';
      input.value = value === undefined || value === null ? '' : String(value);
    }

    if (input.type !== 'checkbox') input.id = `field-${field.name}`;
    if (field.type === 'number') {
      if (field.min !== null && field.min !== undefined) input.min = String(field.min);
      if (field.max !== null && field.max !== undefined) input.max = String(field.max);
    }
    if (field.maxLength) input.maxLength = field.maxLength;
    wrapper.append(input);

    if (field.hint) wrapper.append(el('span', 'hint', field.hint));

    // فیلد تصویر: بارگذاری فایل + نمایش مسیر ذخیره‌شده
    if (field.type === 'image' || (field.type === 'text' && /image|photo|logo/i.test(field.name))) {
      const imageBox = el('div', 'image-field');
      const preview = el('div', 'image-field__preview', 'تصویری انتخاب نشده');
      const row = el('div', 'image-field__row');
      const fileInput = el('input');
      fileInput.type = 'file';
      fileInput.accept = 'image/png,image/jpeg,image/webp';
      fileInput.style.maxWidth = '260px';
      const uploadButton = el('button', 'btn btn--sm btn--ghost', 'بارگذاری تصویر');
      uploadButton.type = 'button';
      const uploadStatus = el('span', 'help-text', '');
      row.append(fileInput, uploadButton, uploadStatus);
      imageBox.append(preview, row);
      wrapper.append(imageBox);

      const refreshPreview = () => {
        const path = input.value;
        clear(preview);
        if (!path) {
          preview.textContent = 'تصویری انتخاب نشده';
          return;
        }
        const image = el('img');
        image.src = path;
        image.alt = field.label;
        preview.append(image);
      };
      refreshPreview();
      input.addEventListener('change', refreshPreview);

      uploadButton.addEventListener('click', async () => {
        if (!fileInput.files || fileInput.files.length === 0) {
          uploadStatus.textContent = 'ابتدا یک فایل انتخاب کنید.';
          return;
        }
        uploadStatus.textContent = 'در حال بارگذاری…';
        try {
          const response = await api.upload('/api/admin/uploads', fileInput.files[0]);
          input.value = response.path;
          refreshPreview();
          uploadStatus.textContent = 'تصویر بارگذاری شد.';
        } catch (error) {
          uploadStatus.textContent = error.message;
        }
      });
    }

    const errorNode = el('span', 'form-row__error', '');
    wrapper.append(errorNode);
    return wrapper;
  }

  function readFormValues(form, meta) {
    const values = {};
    meta.fields.forEach((field) => {
      const wrapper = form.querySelector(`[data-field="${field.name}"]`);
      if (!wrapper) return;
      const input = wrapper.querySelector('input, select, textarea');
      if (!input) return;
      if (field.type === 'boolean') values[field.name] = input.checked;
      else values[field.name] = input.value;
    });
    return values;
  }

  function clearFormErrors(form) {
    form.querySelectorAll('.form-row--error').forEach((row) => row.classList.remove('form-row--error'));
    form.querySelectorAll('.form-row__error').forEach((node) => {
      node.textContent = '';
    });
  }

  function applyFormErrors(form, details) {
    Object.entries(details || {}).forEach(([name, message]) => {
      const wrapper = form.querySelector(`[data-field="${name}"]`);
      if (!wrapper) return;
      wrapper.classList.add('form-row--error');
      const errorNode = wrapper.querySelector('.form-row__error');
      if (errorNode) errorNode.textContent = message;
    });
  }

  function cellValue(item, column, meta) {
    const field = (meta.fields || []).find((entry) => entry.name === column);
    const label = field ? field.label : column;
    const value = item[column];
    if (field && field.type === 'boolean') return value ? 'فعال' : 'غیرفعال';
    if (field && field.type === 'datetime') return value ? formatDate(value, true) : '—';
    if (column === 'last_login_at') return value ? formatDate(value, true) : '—';
    if (column === 'views' || column === 'sort_order') return formatNumber(value);
    if (value === null || value === undefined || value === '') return '—';
    if (column === 'password') return '••••••';
    return String(value);
  }

  async function renderEntityPage(container, entityKey) {
    const metaResponse = await api.get(`/api/admin/meta/${entityKey}`);
    const meta = metaResponse.meta;

    const panel = el('section', 'panel');
    const head = el('div', 'panel__head');
    const headText = el('div');
    headText.append(el('h2', null, meta.title));
    if (meta.description) headText.append(el('p', 'help-text', meta.description));
    head.append(headText);

    const addButton = el('button', 'btn btn--turquoise btn--sm', 'افزودن مورد جدید');
    addButton.type = 'button';
    head.append(addButton);
    panel.append(head);

    const alertBox = alertInto(panel);

    const toolbar = el('div', 'toolbar');
    const search = el('input');
    search.type = 'text';
    search.placeholder = 'جست‌وجو…';
    const searchButton = el('button', 'btn btn--ghost btn--sm', 'جست‌وجو');
    searchButton.type = 'button';
    const countNode = el('span', 'help-text', '');
    toolbar.append(search, searchButton, countNode);
    panel.append(toolbar);

    const tableContainer = el('div', 'table-wrap');
    panel.append(tableContainer);

    const formPanel = el('section', 'panel');
    formPanel.hidden = true;
    panel.after(formPanel);
    container.append(panel);

    let editingId = null;

    function openForm(item) {
      editingId = item ? item.id : null;
      clear(formPanel);
      const formHead = el('div', 'panel__head');
      formHead.append(el('h2', null, item ? `ویرایش: ${item[meta.columns[1]] || item.id}` : `افزودن به ${meta.title}`));
      formPanel.append(formHead);

      const form = el('form', 'form-panel');
      const grid = el('div', 'form-grid form-grid--wide');
      meta.fields.forEach((field) => {
        let value;
        if (item) {
          value = field.type === 'datetime' ? item[`${field.name}_input`] : item[field.name];
          if (field.type === 'password') value = '';
        } else {
          value = field.default === null ? '' : field.default;
        }
        const wrapper = fieldInput(field, value);
        if (field.type === 'textarea' || field.type === 'password') wrapper.classList.add('form-row--full');
        grid.append(wrapper);
      });
      form.append(grid);

      const errorNode = el('div', 'alert alert--error');
      errorNode.hidden = true;
      form.append(errorNode);

      const actions = el('div', 'form-actions');
      const save = el('button', 'btn btn--turquoise', 'ذخیره');
      save.type = 'submit';
      const cancel = el('button', 'btn btn--ghost', 'انصراف');
      cancel.type = 'button';
      cancel.addEventListener('click', () => {
        formPanel.hidden = true;
      });
      actions.append(save, cancel);
      form.append(actions);

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        clearFormErrors(form);
        hideMessage(errorNode);
        save.disabled = true;
        try {
          const values = readFormValues(form, meta);
          if (editingId) {
            await api.put(`/api/admin/${entityKey}/${editingId}`, values);
          } else {
            await api.post(`/api/admin/${entityKey}`, values);
          }
          showMessage(alertBox, 'success', 'تغییرات ذخیره شد.');
          formPanel.hidden = true;
          await loadList();
        } catch (error) {
          if (error.details) applyFormErrors(form, error.details);
          showMessage(errorNode, 'error', error.message);
        } finally {
          save.disabled = false;
        }
      });

      formPanel.append(form);
      formPanel.hidden = false;
      formPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    async function toggleActive(item, fieldName) {
      const values = {};
      values[fieldName] = item[fieldName] ? false : true;
      await api.patch(`/api/admin/${entityKey}/${item.id}`, values);
      await loadList();
    }

    function renderRows(items) {
      clear(tableContainer);
      if (items.length === 0) {
        emptyState(tableContainer, 'موردی ثبت نشده است', 'با دکمه «افزودن مورد جدید» شروع کنید.');
        return;
      }
      const table = el('table', 'data');
      const thead = el('thead');
      const headRow = el('tr');
      meta.columns.forEach((column) => {
        const field = (meta.fields || []).find((entry) => entry.name === column);
        headRow.append(el('th', null, field ? field.label : column));
      });
      headRow.append(el('th', null, 'عملیات'));
      thead.append(headRow);

      const tbody = el('tbody');
      items.forEach((item) => {
        const row = el('tr');
        meta.columns.forEach((column) => row.append(el('td', null, cellValue(item, column, meta))));
        const actionsCell = el('td', 'actions');
        const actions = el('div', 'row-actions');

        const edit = el('button', 'btn btn--ghost btn--sm', 'ویرایش');
        edit.type = 'button';
        edit.addEventListener('click', () => openForm(item));
        actions.append(edit);

        if (meta.fields.some((field) => field.name === 'is_active')) {
          const toggle = el('button', 'btn btn--ghost btn--sm', item.is_active ? 'غیرفعال‌سازی' : 'فعال‌سازی');
          toggle.type = 'button';
          toggle.addEventListener('click', async () => {
            try {
              await toggleActive(item, 'is_active');
            } catch (error) {
              showMessage(alertBox, 'error', error.message);
            }
          });
          actions.append(toggle);
        }
        if (meta.fields.some((field) => field.name === 'is_published')) {
          const publish = el('button', 'btn btn--ghost btn--sm', item.is_published ? 'لغو انتشار' : 'انتشار');
          publish.type = 'button';
          publish.addEventListener('click', async () => {
            try {
              await api.patch(`/api/admin/${entityKey}/${item.id}`, { is_published: !item.is_published });
              await loadList();
            } catch (error) {
              showMessage(alertBox, 'error', error.message);
            }
          });
          actions.append(publish);
        }

        const remove = el('button', 'btn btn--danger btn--sm', 'حذف');
        remove.type = 'button';
        remove.addEventListener('click', async () => {
          const confirmed = await askConfirm('این مورد برای همیشه حذف می‌شود. مطمئن هستید؟');
          if (!confirmed) return;
          try {
            await api.del(`/api/admin/${entityKey}/${item.id}`);
            showMessage(alertBox, 'success', 'مورد حذف شد.');
            await loadList();
          } catch (error) {
            showMessage(alertBox, 'error', error.message);
          }
        });
        actions.append(remove);
        actionsCell.append(actions);
        row.append(actionsCell);
        tbody.append(row);
      });

      table.append(thead, tbody);
      tableContainer.append(table);
    }

    async function loadList() {
      const query = search.value.trim();
      const response = await api.get(
        `/api/admin/${entityKey}?q=${encodeURIComponent(query)}&perPage=200`
      );
      countNode.textContent = `${formatNumber(response.total)} مورد`;
      renderRows(response.items);
    }

    addButton.addEventListener('click', () => openForm(null));
    searchButton.addEventListener('click', loadList);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        loadList();
      }
    });

    await loadList();
  }

  /* ------------------------ مدیریت پیش‌ثبت‌نام‌ها ------------------------ */

  async function renderPreRegistrations(container) {
    const panel = el('section', 'panel');
    const head = el('div', 'panel__head');
    const headText = el('div');
    headText.append(el('h2', null, 'درخواست‌های پیش‌ثبت‌نام'));
    headText.append(
      el('p', 'help-text', 'اطلاعات شخصی دانش‌آموزان فقط در پنل مدیریت و برای مدیران مجاز نمایش داده می‌شود.')
    );
    head.append(headText);

    const exportLink = el('a', 'btn btn--ghost btn--sm', 'خروجی CSV');
    exportLink.href = '/api/admin/pre-registrations/export.csv';
    head.append(exportLink);
    panel.append(head);

    const alertBox = alertInto(panel);

    const tabs = el('div', 'tabbar');
    const filters = [
      { key: '', label: 'همه' },
      { key: 'new', label: 'جدید' },
      { key: 'reviewing', label: 'در حال بررسی' },
      { key: 'approved', label: 'تأییدشده' },
      { key: 'rejected', label: 'ردشده' },
      { key: 'archived', label: 'بایگانی' },
    ];
    panel.append(tabs);

    const toolbar = el('div', 'toolbar');
    const search = el('input');
    search.type = 'text';
    search.placeholder = 'نام، موبایل یا کد پیگیری…';
    const searchButton = el('button', 'btn btn--ghost btn--sm', 'جست‌وجو');
    searchButton.type = 'button';
    const countNode = el('span', 'help-text', '');
    toolbar.append(search, searchButton, countNode);
    panel.append(toolbar);

    const tableContainer = el('div', 'table-wrap');
    panel.append(tableContainer);
    container.append(panel);

    let status = '';
    let items = [];

    function renderTabs() {
      clear(tabs);
      filters.forEach((filter) => {
        const button = el('button', null, filter.label);
        button.type = 'button';
        if (filter.key === status) button.setAttribute('aria-selected', 'true');
        button.addEventListener('click', () => {
          status = filter.key;
          renderTabs();
          loadList();
        });
        tabs.append(button);
      });
    }

    async function openDetail(item) {
      const detail = await api.get(`/api/admin/pre-registrations/${item.id}`);
      const row = detail.item;
      const drawer = el('div', 'drawer');
      const box = el('div', 'drawer__box');
      const drawerHead = el('div', 'drawer__head');
      drawerHead.append(el('h3', null, `درخواست ${row.student_name}`));
      const close = el('button', 'btn btn--ghost btn--sm', 'بستن');
      close.type = 'button';
      close.addEventListener('click', () => drawer.remove());
      drawerHead.append(close);
      box.append(drawerHead);

      const list = el('dl', 'kv');
      const rows = [
        ['کد پیگیری', row.tracking_code],
        ['نام دانش‌آموز', row.student_name],
        ['شماره موبایل', toPersianDigits(row.mobile)],
        ['پایه تحصیلی', row.grade_title],
        ['رشته تحصیلی', row.field_title],
        ['نام ولی', row.parent_name],
        ['توضیحات', row.note || '—'],
        ['زمان ثبت', formatDate(row.created_at, true)],
        ['زمان بررسی', row.reviewed_at ? formatDate(row.reviewed_at, true) : 'بررسی نشده'],
      ];
      rows.forEach(([label, value]) => {
        const line = el('div', 'kv__row');
        line.append(el('dt', null, label));
        line.append(el('dd', null, value));
        list.append(line);
      });
      box.append(list);

      const form = el('form', 'form-panel');
      form.style.marginTop = '1rem';

      const statusRow = el('div', 'form-row');
      statusRow.append(el('label', null, 'وضعیت درخواست'));
      const select = el('select');
      Object.entries(STATUS_LABELS).forEach(([key, label]) => {
        const option = el('option', null, label);
        option.value = key;
        if (row.status === key) option.selected = true;
        select.append(option);
      });
      statusRow.append(select);
      form.append(statusRow);

      const noteRow = el('div', 'form-row');
      noteRow.append(el('label', null, 'یادداشت داخلی / پیام قابل مشاهده برای خانواده'));
      const note = el('textarea');
      note.value = row.admin_note || '';
      noteRow.append(note);
      noteRow.append(
        el('span', 'hint', 'این یادداشت هنگام پیگیری درخواست توسط خانواده نمایش داده می‌شود؛ اطلاعات حساس ننویسید.')
      );
      form.append(noteRow);

      const actions = el('div', 'form-actions');
      const save = el('button', 'btn btn--turquoise', 'ذخیره تغییرات');
      save.type = 'submit';
      const archive = el('button', 'btn btn--ghost', 'بایگانی');
      archive.type = 'button';
      const remove = el('button', 'btn btn--danger', 'حذف درخواست');
      remove.type = 'button';
      actions.append(save, archive);
      if (session.admin.role === 'owner') actions.append(remove);
      form.append(actions);

      const message = el('div', 'alert');
      message.hidden = true;
      form.append(message);

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        hideMessage(message);
        save.disabled = true;
        try {
          await api.patch(`/api/admin/pre-registrations/${row.id}`, {
            status: select.value,
            admin_note: note.value,
          });
          showMessage(message, 'success', 'وضعیت ذخیره شد.');
          await loadList();
        } catch (error) {
          showMessage(message, 'error', error.message);
        } finally {
          save.disabled = false;
        }
      });

      archive.addEventListener('click', async () => {
        try {
          await api.patch(`/api/admin/pre-registrations/${row.id}`, { status: 'archived' });
          showMessage(message, 'success', 'درخواست بایگانی شد.');
          select.value = 'archived';
          await loadList();
        } catch (error) {
          showMessage(message, 'error', error.message);
        }
      });

      remove.addEventListener('click', async () => {
        const confirmed = await askConfirm('این درخواست برای همیشه حذف می‌شود. مطمئن هستید؟');
        if (!confirmed) return;
        try {
          await api.del(`/api/admin/pre-registrations/${row.id}`);
          drawer.remove();
          showMessage(alertBox, 'success', 'درخواست حذف شد.');
          await loadList();
        } catch (error) {
          showMessage(message, 'error', error.message);
        }
      });

      box.append(form);
      drawer.append(box);
      drawer.addEventListener('click', (event) => {
        if (event.target === drawer) drawer.remove();
      });
      document.body.append(drawer);
    }

    function renderTable() {
      clear(tableContainer);
      if (items.length === 0) {
        emptyState(tableContainer, 'درخواستی با این مشخصات یافت نشد', 'فیلتر یا عبارت جست‌وجو را تغییر دهید.');
        return;
      }
      const table = el('table', 'data');
      const thead = el('thead');
      const headRow = el('tr');
      ['دانش‌آموز', 'موبایل', 'پایه', 'رشته', 'کد پیگیری', 'وضعیت', 'زمان ثبت', 'عملیات'].forEach((label) =>
        headRow.append(el('th', null, label))
      );
      thead.append(headRow);
      const tbody = el('tbody');
      items.forEach((item) => {
        const row = el('tr');
        row.append(el('td', null, item.student_name));
        row.append(el('td', null, toPersianDigits(item.mobile)));
        row.append(el('td', null, item.grade_title));
        row.append(el('td', null, item.field_title));
        row.append(el('td', 'mono', item.tracking_code));
        const statusCell = el('td');
        statusCell.append(el('span', `badge badge--${item.status}`, STATUS_LABELS[item.status] || item.status));
        row.append(statusCell);
        row.append(el('td', null, formatDate(item.created_at)));
        const actionsCell = el('td', 'actions');
        const actions = el('div', 'row-actions');
        const details = el('button', 'btn btn--ghost btn--sm', 'جزئیات و بررسی');
        details.type = 'button';
        details.addEventListener('click', () => openDetail(item));
        actions.append(details);
        actionsCell.append(actions);
        row.append(actionsCell);
        tbody.append(row);
      });
      table.append(thead, tbody);
      tableContainer.append(table);
    }

    async function loadList() {
      const query = search.value.trim();
      const response = await api.get(
        `/api/admin/pre-registrations?status=${encodeURIComponent(status)}&q=${encodeURIComponent(query)}&perPage=50`
      );
      items = response.items;
      countNode.textContent = `${formatNumber(response.total)} درخواست — جدید: ${formatNumber(response.counts.new)}`;
      renderTable();
    }

    searchButton.addEventListener('click', loadList);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        loadList();
      }
    });

    renderTabs();
    await loadList();
  }

  /* ------------------------- اطلاعات و تنظیمات ------------------------- */

  const SCHOOL_FIELD_LABELS = {
    name: 'نام مدرسه',
    slogan: 'شعار مدرسه',
    about_short: 'معرفی کوتاه (چند خط)',
    about_long: 'معرفی کامل مدرسه',
    address: 'نشانی',
    phone: 'تلفن',
    mobile: 'موبایل',
    email: 'ایمیل',
    fax: 'نمابر',
    office_hours: 'ساعات پاسخ‌گویی',
    map_url: 'نشانی نقشه (آدرس اینترنتی)',
    logo_path: 'نشان (لوگو)',
    hero_title: 'عنوان بخش اصلی صفحه اول',
    hero_subtitle: 'توضیح زیر عنوان صفحه اول',
    hero_image_path: 'تصویر پس‌زمینه صفحه اول',
    hero_cta_label: 'متن دکمه صفحه اول',
    hero_cta_url: 'آدرس دکمه صفحه اول',
    instagram_url: 'اینستاگرام',
    telegram_url: 'تلگرام',
    whatsapp_url: 'واتساپ',
    aparat_url: 'آپارات',
  };

  const SCHOOL_MULTILINE = ['about_short', 'about_long', 'address', 'office_hours'];

  async function renderSchoolInfo(container) {
    const data = await api.get('/api/admin/school-info');
    const panel = el('section', 'panel');
    panel.append(el('h2', null, 'اطلاعات مدرسه'));
    panel.append(
      el(
        'p',
        'help-text',
        'هر اطلاعاتی که هنوز تأیید نشده است، خالی بگذارید؛ همان‌جا در سایت به صورت «تکمیل نشده» نمایش داده می‌شود و اطلاعات نادرست منتشر نمی‌شود.'
      )
    );

    const alertBox = alertInto(panel);
    const form = el('form', 'form-panel');
    const grid = el('div', 'form-grid form-grid--wide');

    data.fields.forEach((fieldName) => {
      const row = el('div', 'form-row');
      row.dataset.field = fieldName;
      row.append(el('label', null, SCHOOL_FIELD_LABELS[fieldName] || fieldName));
      let input;
      if (SCHOOL_MULTILINE.includes(fieldName)) {
        input = el('textarea');
        input.value = data.info[fieldName] || '';
        row.classList.add('form-row--full');
      } else if (/image|logo/i.test(fieldName)) {
        const box = el('div', 'image-field');
        const preview = el('div', 'image-field__preview', 'تصویری انتخاب نشده');
        input = el('input');
        input.type = 'text';
        input.value = data.info[fieldName] || '';
        input.placeholder = '/uploads/…';
        const fileRow = el('div', 'image-field__row');
        const fileInput = el('input');
        fileInput.type = 'file';
        fileInput.accept = 'image/png,image/jpeg,image/webp';
        fileInput.style.maxWidth = '240px';
        const uploadButton = el('button', 'btn btn--sm btn--ghost', 'بارگذاری');
        uploadButton.type = 'button';
        const status = el('span', 'help-text', '');
        fileRow.append(fileInput, uploadButton, status);
        box.append(preview, input, fileRow);
        row.append(box);
        const refresh = () => {
          clear(preview);
          if (!input.value) {
            preview.textContent = 'تصویری انتخاب نشده';
            return;
          }
          const image = el('img');
          image.src = input.value;
          image.alt = SCHOOL_FIELD_LABELS[fieldName] || fieldName;
          preview.append(image);
        };
        refresh();
        uploadButton.addEventListener('click', async () => {
          if (!fileInput.files || fileInput.files.length === 0) {
            status.textContent = 'ابتدا یک فایل انتخاب کنید.';
            return;
          }
          status.textContent = 'در حال بارگذاری…';
          try {
            const response = await api.upload('/api/admin/uploads', fileInput.files[0]);
            input.value = response.path;
            refresh();
            status.textContent = 'بارگذاری شد.';
          } catch (error) {
            status.textContent = error.message;
          }
        });
        grid.append(row);
        return;
      } else {
        input = el('input');
        input.type = 'text';
        input.value = data.info[fieldName] || '';
      }
      row.append(input);
      const errorNode = el('span', 'form-row__error', '');
      row.append(errorNode);
      grid.append(row);
    });

    form.append(grid);
    const actions = el('div', 'form-actions');
    const save = el('button', 'btn btn--turquoise', 'ذخیره اطلاعات');
    save.type = 'submit';
    actions.append(save);
    form.append(actions);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      form.querySelectorAll('.form-row__error').forEach((node) => {
        node.textContent = '';
      });
      hideMessage(alertBox);
      save.disabled = true;
      const payload = {};
      data.fields.forEach((fieldName) => {
        const row = form.querySelector(`[data-field="${fieldName}"]`);
        if (!row) return;
        const input = row.querySelector('input[type="text"], textarea');
        if (input) payload[fieldName] = input.value;
      });
      try {
        const response = await api.put('/api/admin/school-info', payload);
        showMessage(
          alertBox,
          response.missing.length > 0 ? 'warning' : 'success',
          response.missing.length > 0
            ? `ذخیره شد. اطلاعات زیر هنوز تکمیل نشده است: ${response.missing.join('، ')}`
            : 'اطلاعات مدرسه ذخیره شد.'
        );
      } catch (error) {
        if (error.details) {
          Object.entries(error.details).forEach(([name, message]) => {
            const node = form.querySelector(`[data-field="${name}"] .form-row__error`);
            if (node) node.textContent = message;
          });
        }
        showMessage(alertBox, 'error', error.message);
      } finally {
        save.disabled = false;
      }
    });

    panel.append(form);
    container.append(panel);
  }

  async function renderSettings(container) {
    const data = await api.get('/api/admin/settings');
    const panel = el('section', 'panel');
    panel.append(el('h2', null, 'تنظیمات عمومی سایت'));
    const alertBox = alertInto(panel);
    const form = el('form', 'form-panel');
    const grid = el('div', 'form-grid form-grid--wide');

    data.definitions.forEach((definition) => {
      const row = el('div', 'form-row');
      if (definition.type === 'boolean') {
        const label = el('label', 'checkbox-row');
        const input = el('input');
        input.type = 'checkbox';
        input.checked = Boolean(data.settings[definition.key]);
        input.dataset.settingKey = definition.key;
        label.append(input);
        label.append(el('span', null, definition.label));
        row.append(label);
      } else {
        row.append(el('label', null, definition.label));
        const input = definition.type === 'textarea' ? el('textarea') : el('input');
        if (input.tagName === 'INPUT') input.type = 'text';
        input.value = data.settings[definition.key] || '';
        input.dataset.settingKey = definition.key;
        row.append(input);
        if (definition.type === 'textarea') row.classList.add('form-row--full');
      }
      grid.append(row);
    });

    form.append(grid);
    const actions = el('div', 'form-actions');
    const save = el('button', 'btn btn--turquoise', 'ذخیره تنظیمات');
    save.type = 'submit';
    actions.append(save);
    form.append(actions);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      hideMessage(alertBox);
      save.disabled = true;
      const payload = {};
      form.querySelectorAll('[data-setting-key]').forEach((input) => {
        payload[input.dataset.settingKey] = input.type === 'checkbox' ? input.checked : input.value;
      });
      try {
        await api.put('/api/admin/settings', payload);
        showMessage(alertBox, 'success', 'تنظیمات ذخیره شد.');
      } catch (error) {
        showMessage(alertBox, 'error', error.message);
      } finally {
        save.disabled = false;
      }
    });

    panel.append(form);
    container.append(panel);
  }

  /* ------------------------------ پیامک ------------------------------ */

  async function renderSms(container) {
    const data = await api.get('/api/admin/settings/sms');
    const statusPanel = el('section', 'panel');
    statusPanel.append(el('h2', null, 'وضعیت سرویس پیامک'));
    const statusAlert = el('div', `alert alert--${data.status.enabled ? 'success' : 'warning'}`);
    statusAlert.textContent = data.status.message;
    statusPanel.append(statusAlert);
    if (data.sms.source === 'env') {
      statusPanel.append(
        el(
          'p',
          'help-text',
          'تنظیمات از متغیرهای محیطی سرور خوانده می‌شود؛ مقادیر این فرم فقط برای مرجع نمایش داده می‌شوند.'
        )
      );
    }
    if (data.sms.last_tested_at) {
      statusPanel.append(
        el(
          'p',
          'help-text',
          `آخرین آزمایش: ${formatDate(data.sms.last_tested_at, true)} — ${data.sms.last_test_result || 'بدون نتیجه'}`
        )
      );
    }
    container.append(statusPanel);

    const panel = el('section', 'panel');
    panel.append(el('h2', null, 'تنظیمات سرویس پیامک'));
    panel.append(
      el(
        'p',
        'help-text',
        'برای ارسال واقعی پیامک، حساب فعال یکی از ارائه‌دهندگان و اعتبارنامه آن لازم است. کلید API فقط در سرور نگهداری می‌شود و در مرورگر نمایش داده نمی‌شود.'
      )
    );
    const alertBox = alertInto(panel);
    const form = el('form', 'form-panel');
    const grid = el('div', 'form-grid');

    const providerRow = el('div', 'form-row');
    providerRow.append(el('label', null, 'ارائه‌دهنده'));
    const providerSelect = el('select');
    const emptyOption = el('option', null, '— انتخاب نشده —');
    emptyOption.value = '';
    providerSelect.append(emptyOption);
    data.providers.forEach((provider) => {
      const option = el('option', null, provider.label);
      option.value = provider.key;
      if (data.sms.provider === provider.key) option.selected = true;
      providerSelect.append(option);
    });
    providerRow.append(providerSelect);
    grid.append(providerRow);

    const apiKeyRow = el('div', 'form-row');
    apiKeyRow.append(el('label', null, 'کلید API / رمز سرویس'));
    const apiKeyInput = el('input');
    apiKeyInput.type = 'password';
    apiKeyInput.placeholder = data.sms.hasApiKey ? `ذخیره‌شده: ${data.sms.apiKeyMasked}` : 'وارد نشده';
    apiKeyInput.autocomplete = 'new-password';
    apiKeyRow.append(apiKeyInput);
    apiKeyRow.append(el('span', 'hint', 'برای تغییر مقدار جدید وارد کنید؛ خالی گذاشتن یعنی تغییر نکند.'));
    grid.append(apiKeyRow);

    const usernameRow = el('div', 'form-row');
    usernameRow.append(el('label', null, 'نام کاربری سرویس (در صورت نیاز)'));
    const usernameInput = el('input');
    usernameInput.type = 'text';
    usernameInput.value = data.sms.username || '';
    usernameRow.append(usernameInput);
    grid.append(usernameRow);

    const senderRow = el('div', 'form-row');
    senderRow.append(el('label', null, 'شماره فرستنده'));
    const senderInput = el('input');
    senderInput.type = 'text';
    senderInput.value = data.sms.sender_number || '';
    senderRow.append(senderInput);
    grid.append(senderRow);

    const templateRow = el('div', 'form-row form-row--full');
    templateRow.append(el('label', null, 'متن پیامک کد ورود'));
    const templateInput = el('textarea');
    templateInput.value = data.sms.otp_template || '';
    templateRow.append(templateInput);
    templateRow.append(el('span', 'hint', 'برای جای‌گذاری کد از {code} استفاده کنید.'));
    grid.append(templateRow);

    const enabledRow = el('div', 'form-row');
    const enabledLabel = el('label', 'checkbox-row');
    const enabledInput = el('input');
    enabledInput.type = 'checkbox';
    enabledInput.checked = Boolean(data.sms.is_enabled);
    enabledLabel.append(enabledInput);
    enabledLabel.append(el('span', null, 'ارسال پیامک فعال باشد'));
    enabledRow.append(enabledLabel);
    grid.append(enabledRow);

    form.append(grid);

    const actions = el('div', 'form-actions');
    const save = el('button', 'btn btn--turquoise', 'ذخیره تنظیمات');
    save.type = 'submit';
    const test = el('button', 'btn btn--ghost', 'آزمایش اتصال');
    test.type = 'button';
    const testResult = el('span', 'help-text', '');
    actions.append(save, test, testResult);
    form.append(actions);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      hideMessage(alertBox);
      save.disabled = true;
      try {
        const payload = {
          provider: providerSelect.value,
          username: usernameInput.value,
          sender_number: senderInput.value,
          otp_template: templateInput.value,
          is_enabled: enabledInput.checked,
        };
        if (apiKeyInput.value.trim()) payload.api_key = apiKeyInput.value.trim();
        const response = await api.put('/api/admin/settings/sms', payload);
        showMessage(
          alertBox,
          response.status.enabled ? 'success' : 'warning',
          `${response.message} وضعیت: ${response.status.message}`
        );
        apiKeyInput.value = '';
      } catch (error) {
        showMessage(alertBox, 'error', error.message);
      } finally {
        save.disabled = false;
      }
    });

    test.addEventListener('click', async () => {
      test.disabled = true;
      testResult.textContent = 'در حال آزمایش…';
      try {
        const response = await api.post('/api/admin/settings/sms/test');
        testResult.textContent = response.result.message;
        showMessage(alertBox, response.result.ok ? 'success' : 'warning', response.result.message);
      } catch (error) {
        testResult.textContent = '';
        showMessage(alertBox, 'error', error.message);
      } finally {
        test.disabled = false;
      }
    });

    panel.append(form);
    container.append(panel);
  }

  /* ------------------------------ کاربران ------------------------------ */

  async function renderUsers(container) {
    const panel = el('section', 'panel');
    const head = el('div', 'panel__head');
    head.append(el('h2', null, 'کاربران وارد‌شده با شماره موبایل'));
    panel.append(head);
    const alertBox = alertInto(panel);

    const toolbar = el('div', 'toolbar');
    const search = el('input');
    search.type = 'text';
    search.placeholder = 'جست‌وجوی موبایل…';
    const searchButton = el('button', 'btn btn--ghost btn--sm', 'جست‌وجو');
    searchButton.type = 'button';
    toolbar.append(search, searchButton);
    panel.append(toolbar);

    const tableContainer = el('div', 'table-wrap');
    panel.append(tableContainer);
    container.append(panel);

    async function loadList() {
      const response = await api.get(`/api/admin/users?q=${encodeURIComponent(search.value.trim())}`);
      clear(tableContainer);
      if (response.items.length === 0) {
        emptyState(tableContainer, 'کاربری یافت نشد', 'کاربران پس از ورود با کد پیامکی در این فهرست نمایش داده می‌شوند.');
        return;
      }
      const table = el('table', 'data');
      const thead = el('thead');
      const headRow = el('tr');
      ['موبایل', 'نام', 'تأیید موبایل', 'آخرین ورود', 'تعداد درخواست', 'وضعیت', 'عملیات'].forEach((label) =>
        headRow.append(el('th', null, label))
      );
      thead.append(headRow);
      const tbody = el('tbody');
      response.items.forEach((user) => {
        const row = el('tr');
        row.append(el('td', null, toPersianDigits(user.mobile)));
        row.append(el('td', null, user.full_name || '—'));
        row.append(el('td', null, user.mobile_verified_at ? formatDate(user.mobile_verified_at, true) : '—'));
        row.append(el('td', null, user.last_login_at ? formatDate(user.last_login_at, true) : '—'));
        row.append(el('td', null, formatNumber(user.requests_total)));
        const statusCell = el('td');
        statusCell.append(
          el('span', `badge badge--${user.is_blocked ? 'rejected' : 'approved'}`, user.is_blocked ? 'مسدود' : 'فعال')
        );
        row.append(statusCell);
        const actionsCell = el('td', 'actions');
        const button = el('button', 'btn btn--ghost btn--sm', user.is_blocked ? 'رفع مسدودی' : 'مسدودسازی');
        button.type = 'button';
        button.addEventListener('click', async () => {
          try {
            await api.patch(`/api/admin/users/${user.id}`, { is_blocked: !user.is_blocked });
            await loadList();
          } catch (error) {
            showMessage(alertBox, 'error', error.message);
          }
        });
        actionsCell.append(button);
        row.append(actionsCell);
        tbody.append(row);
      });
      table.append(thead, tbody);
      tableContainer.append(table);
    }

    searchButton.addEventListener('click', loadList);
    await loadList();
  }

  /* --------------------------- گزارش رویدادها --------------------------- */

  async function renderSecurity(container) {
    const panel = el('section', 'panel');
    panel.append(el('h2', null, 'گزارش رویدادهای امنیتی و مدیریتی'));
    panel.append(
      el(
        'p',
        'help-text',
        'در این گزارش هیچ رمز عبور، کد یک‌بارمصرف یا کلید API ثبت نمی‌شود. آی‌پی‌ها تنها برای پیگیری رویدادها نگهداری می‌شوند.'
      )
    );
    const tableContainer = el('div', 'table-wrap');
    panel.append(tableContainer);
    container.append(panel);

    const data = await api.get('/api/admin/audit-logs?limit=150');
    if (data.items.length === 0) {
      emptyState(tableContainer, 'رویدادی ثبت نشده است', 'پس از انجام عملیات مدیریتی، رویدادها اینجا نمایش داده می‌شوند.');
      return;
    }
    const table = el('table', 'data');
    const thead = el('thead');
    const headRow = el('tr');
    ['زمان', 'کاربر', 'رویداد', 'موضوع', 'جزئیات', 'آی‌پی'].forEach((label) => headRow.append(el('th', null, label)));
    thead.append(headRow);
    const tbody = el('tbody');
    data.items.forEach((log) => {
      const row = el('tr');
      row.append(el('td', null, formatDate(log.created_at, true)));
      row.append(el('td', null, log.actor_label || log.actor_type));
      row.append(el('td', null, log.action));
      row.append(el('td', null, `${log.entity || ''} ${log.entity_id || ''}`.trim() || '—'));
      row.append(el('td', null, log.details || '—'));
      row.append(el('td', 'mono', log.ip || '—'));
      tbody.append(row);
    });
    table.append(thead, tbody);
    tableContainer.append(table);
  }

  /* ---------------------------- تغییر رمز ---------------------------- */

  async function renderPassword(container) {
    const panel = el('section', 'panel');
    panel.append(el('h2', null, 'تغییر رمز عبور حساب جاری'));
    panel.append(
      el('p', 'help-text', 'رمز عبور باید حداقل ۱۰ نویسه و شامل حرف و رقم باشد. با تغییر رمز، سایر دستگاه‌ها از حساب خارج می‌شوند.')
    );
    const alertBox = alertInto(panel);
    const form = el('form', 'form-panel');

    const currentRow = el('div', 'form-row');
    currentRow.append(el('label', null, 'رمز عبور فعلی'));
    const currentInput = el('input');
    currentInput.type = 'password';
    currentInput.autocomplete = 'current-password';
    currentRow.append(currentInput);
    currentRow.append(el('span', 'form-row__error', ''));
    form.append(currentRow);

    const newRow = el('div', 'form-row');
    newRow.append(el('label', null, 'رمز عبور جدید'));
    const newInput = el('input');
    newInput.type = 'password';
    newInput.autocomplete = 'new-password';
    newRow.append(newInput);
    newRow.append(el('span', 'form-row__error', ''));
    form.append(newRow);

    const repeatRow = el('div', 'form-row');
    repeatRow.append(el('label', null, 'تکرار رمز عبور جدید'));
    const repeatInput = el('input');
    repeatInput.type = 'password';
    repeatInput.autocomplete = 'new-password';
    repeatRow.append(repeatInput);
    repeatRow.append(el('span', 'form-row__error', ''));
    form.append(repeatRow);

    const actions = el('div', 'form-actions');
    const save = el('button', 'btn btn--turquoise', 'تغییر رمز عبور');
    save.type = 'submit';
    actions.append(save);
    form.append(actions);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      hideMessage(alertBox);
      form.querySelectorAll('.form-row__error').forEach((node) => {
        node.textContent = '';
      });
      if (newInput.value !== repeatInput.value) {
        form.querySelectorAll('.form-row__error')[1].textContent = 'تکرار رمز عبور مطابقت ندارد.';
        return;
      }
      save.disabled = true;
      try {
        const response = await api.post('/api/admin/password', {
          current_password: currentInput.value,
          new_password: newInput.value,
        });
        showMessage(alertBox, 'success', response.message);
        form.reset();
      } catch (error) {
        if (error.details) {
          const nodes = form.querySelectorAll('.form-row__error');
          if (error.details.current_password) nodes[0].textContent = error.details.current_password;
          if (error.details.new_password) nodes[1].textContent = error.details.new_password;
        }
        showMessage(alertBox, 'error', error.message);
      } finally {
        save.disabled = false;
      }
    });

    panel.append(form);
    container.append(panel);
  }

  /* ---------------------------- راه‌اندازی ---------------------------- */

  async function boot() {
    const page = document.body.dataset.adminPage || 'index';
    const root = document.getElementById('admin-shell');
    if (!root) return;

    try {
      const sessionResponse = await api.get('/api/auth/session');
      if (sessionResponse.principal !== 'admin') {
        window.location.replace('/admin/login');
        return;
      }
      session.admin = sessionResponse.admin;
      session.csrfToken = sessionResponse.csrfToken;
    } catch (error) {
      window.location.replace('/admin/login');
      return;
    }

    const content = renderShell(page);

    if (session.admin.must_change_password && page !== 'password') {
      const box = el('div', 'alert alert--warning');
      box.append(el('strong', null, 'توجه: '));
      box.append(document.createTextNode('رمز عبور حساب شما باید تغییر کند. '));
      const link = el('a', null, 'صفحه تغییر رمز عبور');
      link.href = '/admin/password';
      box.append(link);
      content.append(box);
    }

    try {
      if (page === 'index') await renderDashboard(content);
      else if (page === 'preregistrations') await renderPreRegistrations(content);
      else if (page === 'school-info') await renderSchoolInfo(content);
      else if (page === 'settings') await renderSettings(content);
      else if (page === 'sms') await renderSms(content);
      else if (page === 'users') await renderUsers(content);
      else if (page === 'security') await renderSecurity(content);
      else if (page === 'password') await renderPassword(content);
      else if (ENTITY_PAGES.includes(page)) await renderEntityPage(content, page);
      else {
        emptyState(content, 'این بخش یافت نشد', 'از فهرست کنار صفحه یک بخش را انتخاب کنید.');
      }
    } catch (error) {
      const box = el('div', 'alert alert--error');
      box.textContent = `بارگذاری این بخش ناموفق بود: ${error.message}`;
      content.append(box);
    }
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
