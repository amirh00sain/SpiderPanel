/* رندر محتوای سایت عمومی از API (هیچ محتوایی در HTML ثابت نشده است) */

(function () {
  'use strict';

  const api = window.SwApi;
  const H = window.SwHelpers;
  const { el, clear, emptyState, formatDate, formatNumber, revealOnScroll } = H;

  const NAV_ITEMS = [
    { href: '/', label: 'صفحه اصلی', page: 'home' },
    { href: '/about', label: 'معرفی مدرسه', page: 'about' },
    { href: '/fields', label: 'رشته‌های تحصیلی', page: 'fields' },
    { href: '/grades', label: 'پایه‌های تحصیلی', page: 'grades' },
    { href: '/news', label: 'اخبار و اطلاعیه‌ها', page: 'news' },
    { href: '/gallery', label: 'گالری تصاویر', page: 'gallery' },
    { href: '/staff', label: 'کادر مدرسه', page: 'staff' },
    { href: '/achievements', label: 'افتخارات', page: 'achievements' },
    { href: '/contact', label: 'ارتباط با ما', page: 'contact' },
  ];

  const state = { data: null };

  async function loadSiteData() {
    if (state.data) return state.data;
    state.data = await api.get('/api/site');
    return state.data;
  }

  function schoolName(data) {
    return (data.school && data.school.name) || (data.settings && data.settings.site_title) || 'دبیرستان';
  }

  /* ------------------------------ چیدمان ------------------------------ */

  function renderTopbar(data) {
    const container = document.getElementById('site-topbar');
    if (!container) return;
    const announcements = data.announcements || [];
    const top = announcements.find((item) => item.is_pinned) || announcements[0];

    const inner = el('div', 'container topbar__inner');
    const left = el('div', 'topbar__links');
    const info = data.school || {};
    if (info.phone) {
      const phone = el('a', null, `تلفن: ${H.toPersianDigits(info.phone)}`);
      phone.href = `tel:${String(info.phone).replace(/\s/g, '')}`;
      left.append(phone);
    }
    if (info.email) {
      const email = el('a', null, info.email);
      email.href = `mailto:${info.email}`;
      left.append(email);
    }
    if (!info.phone && !info.email) {
      left.append(el('span', null, 'اطلاعات تماس در صفحه «ارتباط با ما» منتشر می‌شود.'));
    }

    const right = el('div', 'topbar__notice');
    if (top) {
      right.append(el('strong', null, 'اطلاعیه:'));
      const link = el('a', null, top.title);
      link.href = '/news';
      right.append(link);
    } else {
      const login = el('a', null, 'ورود با شماره موبایل');
      login.href = '/login';
      right.append(login);
    }

    inner.append(left, right);
    container.append(inner);
  }

  function renderHeader(data, activePage) {
    const container = document.getElementById('site-header');
    if (!container) return;
    const info = data.school || {};
    const name = schoolName(data);

    const inner = el('div', 'container site-header__inner');

    const brand = el('a', 'brand');
    brand.href = '/';
    const logoBox = el('div', 'brand__logo');
    if (info.logo_path) {
      const image = el('img');
      image.src = info.logo_path;
      image.alt = `نشان ${name}`;
      logoBox.append(image);
    } else {
      logoBox.innerHTML =
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 6.5 12 3l9 3.5M4.5 9.5h15M6 9.5V17m4-7.5V17m4-7.5V17m4-7.5V17M3.5 19.5h17"/></svg>';
    }
    const text = el('div', 'brand__text');
    text.append(el('strong', null, name));
    text.append(el('span', null, info.slogan || 'دبیرستان دوره دوم'));
    brand.append(logoBox, text);

    const nav = el('nav', 'nav');
    nav.id = 'main-nav';
    nav.setAttribute('aria-label', 'فهرست اصلی');
    NAV_ITEMS.forEach((item) => {
      const link = el('a', null, item.label);
      link.href = item.href;
      if (item.page === activePage) link.setAttribute('aria-current', 'page');
      nav.append(link);
    });

    const actions = el('div', 'header-actions');
    const cta = el('a', 'btn btn--gold btn--sm', 'پیش‌ثبت‌نام');
    cta.href = '/pre-registration';
    const toggle = el('button', 'nav-toggle', '☰');
    toggle.type = 'button';
    toggle.setAttribute('aria-label', 'نمایش فهرست');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', () => {
      const open = nav.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', String(open));
    });
    actions.append(cta, toggle);

    inner.append(brand, nav, actions);
    container.append(inner);
  }

  function renderFooter(data) {
    const container = document.getElementById('site-footer');
    if (!container) return;
    const info = data.school || {};
    const settings = data.settings || {};
    const grid = el('div', 'footer-grid');

    const about = el('div');
    about.append(el('h3', null, schoolName(data)));
    about.append(el('p', null, settings.site_description || info.about_short || 'وب‌سایت رسمی مدرسه.'));
    if (settings.footer_note) about.append(el('p', null, settings.footer_note));
    grid.append(about);

    const links = el('div');
    links.append(el('h3', null, 'دسترسی سریع'));
    const list = el('ul', 'footer-links');
    [
      { href: '/about', label: 'معرفی مدرسه' },
      { href: '/fields', label: 'رشته‌های تحصیلی' },
      { href: '/news', label: 'اخبار و اطلاعیه‌ها' },
      { href: '/pre-registration', label: 'پیش‌ثبت‌نام' },
      { href: '/track', label: 'پیگیری پیش‌ثبت‌نام' },
      { href: '/faq', label: 'پرسش‌های متداول' },
    ].forEach((item) => {
      const li = el('li');
      const link = el('a', null, item.label);
      link.href = item.href;
      li.append(link);
      list.append(li);
    });
    links.append(list);
    grid.append(links);

    const contact = el('div');
    contact.append(el('h3', null, 'اطلاعات تماس'));
    const contactList = el('ul', 'footer-links');
    const rows = [
      ['نشانی', info.address],
      ['تلفن', info.phone ? H.toPersianDigits(info.phone) : ''],
      ['ایمیل', info.email],
      ['ساعات پاسخ‌گویی', info.office_hours],
    ].filter((row) => row[1]);
    if (rows.length === 0) {
      const li = el('li', null, settings.contact_notice || 'اطلاعات تماس پس از تأیید منتشر می‌شود.');
      contactList.append(li);
    } else {
      rows.forEach(([label, value]) => contactList.append(el('li', null, `${label}: ${value}`)));
    }
    contact.append(contactList);

    const socials = [
      ['اینستاگرام', info.instagram_url],
      ['تلگرام', info.telegram_url],
      ['واتساپ', info.whatsapp_url],
      ['آپارات', info.aparat_url],
    ].filter((row) => row[1]);
    if (socials.length > 0) {
      const box = el('div', 'social-links');
      socials.forEach(([label, url]) => {
        const link = el('a', null, label);
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        box.append(link);
      });
      contact.append(box);
    }
    grid.append(contact);

    const wrapper = el('div', 'container');
    wrapper.append(grid);
    const bottom = el('div', 'footer-bottom');
    bottom.append(el('span', null, `© ${H.toPersianDigits(new Date().getFullYear())} ${schoolName(data)}`));
    const adminLink = el('a', null, 'ورود مدیران');
    adminLink.href = '/admin/login';
    bottom.append(adminLink);
    wrapper.append(bottom);
    container.append(wrapper);
  }

  /* ------------------------------ بخش‌ها ------------------------------ */

  function renderHero(data) {
    const container = document.getElementById('home-hero');
    if (!container) return;
    const info = data.school || {};
    const settings = data.settings || {};

    if (info.hero_image_path) {
      const image = el('div', 'hero__image');
      image.style.backgroundImage = `url("${info.hero_image_path}")`;
      container.append(image);
    }

    const inner = el('div', 'container hero__inner');
    inner.append(el('span', 'section__kicker', 'دبیرستان دوره دوم'));

    const heading = el('h1', null, info.hero_title || schoolName(data));
    inner.append(heading);
    inner.append(
      el(
        'p',
        null,
        info.hero_subtitle ||
          settings.site_description ||
          'آموزش باکیفیت، محیط آموزشی استاندارد و پرورش دانش‌آموزان برای آینده‌ای روشن.'
      )
    );

    const actions = el('div', 'hero__actions');
    const primary = el('a', 'btn btn--gold', info.hero_cta_label || 'پیش‌ثبت‌نام مدارس');
    primary.href = info.hero_cta_url || '/pre-registration';
    actions.append(primary);

    const secondary = el('a', 'btn btn--ghost', 'معرفی مدرسه');
    secondary.href = '/about';
    secondary.style.color = '#fff';
    secondary.style.borderColor = 'rgba(255,255,255,0.35)';
    actions.append(secondary);
    inner.append(actions);

    const badges = el('div', 'hero__badges');
    const gradeTitles = (data.grades || []).map((grade) => grade.title);
    if (gradeTitles.length > 0) badges.append(el('span', 'hero__badge', gradeTitles.join(' • ')));
    (data.studyFields || []).slice(0, 3).forEach((field) => {
      badges.append(el('span', 'hero__badge', field.title));
    });
    if (badges.childElementCount > 0) inner.append(badges);

    const tracking = el('p', 'hero__note');
    tracking.append(document.createTextNode('درخواست خود را ثبت کرده‌اید؟ '));
    const trackLink = el('a', null, 'وضعیت پیش‌ثبت‌نام را پیگیری کنید');
    trackLink.href = '/track';
    trackLink.style.color = '#e0a800';
    tracking.append(trackLink);
    inner.append(tracking);

    container.append(inner);
  }

  function renderAbout(data) {
    const short = document.getElementById('about-short');
    const long = document.getElementById('about-long');
    const info = data.school || {};
    if (short) {
      if (info.about_short) short.textContent = info.about_short;
      else
        emptyState(
          short,
          'معرفی مدرسه به‌زودی تکمیل می‌شود',
          'این بخش از پنل مدیریت قابل تکمیل است.'
        );
    }
    if (long) {
      const text = info.about_long || '';
      clear(long);
      if (!text) {
        emptyState(long, 'متن کامل معرفی مدرسه', 'این متن از پنل مدیریت وارد می‌شود.');
      } else {
        text.split('\n').filter(Boolean).forEach((paragraph) => long.append(el('p', null, paragraph)));
      }
    }
  }

  function renderGrades(data) {
    const containers = document.querySelectorAll('[data-render="grades"]');
    containers.forEach((container) => {
      clear(container);
      const grades = data.grades || [];
      if (grades.length === 0) {
        emptyState(container, 'پایه‌ای ثبت نشده است', 'پایه‌های تحصیلی از پنل مدیریت اضافه می‌شوند.');
        return;
      }
      grades.forEach((grade) => {
        const card = el('article', 'card card--accent reveal');
        const icon = el('div', 'card__icon', grade.grade_number ? H.toPersianDigits(grade.grade_number) : '●');
        card.append(icon);
        card.append(el('h3', null, grade.title));
        card.append(el('p', null, grade.description || 'برنامه آموزشی و کلاس‌های اختصاصی این پایه.'));
        container.append(card);
      });
    });
  }

  function renderFields(data) {
    const containers = document.querySelectorAll('[data-render="fields"]');
    containers.forEach((container) => {
      clear(container);
      const fields = data.studyFields || [];
      if (fields.length === 0) {
        emptyState(container, 'رشته‌ای ثبت نشده است', 'رشته‌های تحصیلی از پنل مدیریت اضافه می‌شوند.');
        return;
      }
      fields.forEach((field) => {
        const card = el('article', 'field-card reveal');
        if (field.color) card.style.background = field.color;
        card.append(el('span', 'field-card__badge', 'رشته تحصیلی'));
        card.append(el('h3', null, field.title));
        if (field.description) card.append(el('p', null, field.description));
        container.append(card);
      });
    });
  }

  function renderStaff(data) {
    const containers = document.querySelectorAll('[data-render="staff"]');
    const staff = data.staff || [];
    containers.forEach((container) => {
      clear(container);
      if (staff.length === 0) {
        emptyState(
          container,
          'کادر مدرسه ثبت نشده است',
          'نام و سمت مدیران و دبیران پس از دریافت اطلاعات تأییدشده از پنل مدیریت وارد می‌شود.'
        );
        return;
      }
      staff.forEach((member) => {
        const card = el('article', 'card staff-card reveal');
        const photo = el('div', 'staff-card__photo');
        if (member.photo_path) {
          const image = el('img');
          image.src = member.photo_path;
          image.alt = member.full_name;
          image.loading = 'lazy';
          photo.append(image);
        } else {
          photo.textContent = String(member.full_name || '؟').trim().charAt(0);
        }
        card.append(photo);
        card.append(el('h3', null, member.full_name));
        if (member.role_title) card.append(el('div', 'staff-card__role', member.role_title));
        if (member.bio) card.append(el('p', null, member.bio));
        container.append(card);
      });
    });
  }

  function renderAchievements(data) {
    const containers = document.querySelectorAll('[data-render="achievements"]');
    const items = data.achievements || [];
    containers.forEach((container) => {
      clear(container);
      if (items.length === 0) {
        emptyState(container, 'افتخاری ثبت نشده است', 'افتخارات و دستاوردها از پنل مدیریت ثبت می‌شوند.');
        return;
      }
      items.forEach((item) => {
        const box = el('article', 'achievement reveal');
        if (item.year) box.append(el('span', 'achievement__year', item.year));
        const body = el('div');
        body.append(el('h3', null, item.title));
        if (item.description) body.append(el('p', null, item.description));
        box.append(body);
        container.append(box);
      });
    });
  }

  function newsCard(item) {
    const card = el('article', 'news-item reveal');
    const thumb = el('div', 'news-item__thumb');
    if (item.cover_image) {
      const image = el('img');
      image.src = item.cover_image;
      image.alt = item.title;
      image.loading = 'lazy';
      thumb.append(image);
    } else {
      thumb.textContent = 'خبر';
    }
    const body = el('div');
    const title = el('h3');
    const link = el('a', null, item.title);
    link.href = `/news/${encodeURIComponent(item.slug)}`;
    title.append(link);
    body.append(title);
    if (item.summary) body.append(el('p', null, item.summary));
    const meta = el('div', 'news-item__meta');
    meta.append(el('span', null, formatDate(item.published_at || item.created_at)));
    const more = el('a', null, 'ادامه خبر ←');
    more.href = `/news/${encodeURIComponent(item.slug)}`;
    meta.append(more);
    body.append(meta);
    card.append(thumb, body);
    return card;
  }

  function renderNews(data) {
    const container = document.getElementById('news-list');
    if (!container) return;
    clear(container);
    const news = data.news || [];
    if (news.length === 0) {
      emptyState(container, 'خبری منتشر نشده است', 'اخبار و اطلاعیه‌های مدرسه در این بخش نمایش داده می‌شوند.');
      return;
    }
    news.forEach((item) => container.append(newsCard(item)));
  }

  function renderAnnouncements(data) {
    const container = document.getElementById('announcements-list');
    if (!container) return;
    clear(container);
    const items = data.announcements || [];
    if (items.length === 0) {
      emptyState(container, 'اطلاعیه فعالی وجود ندارد', 'اطلاعیه‌های کوتاه مدرسه در این بخش نمایش داده می‌شوند.');
      return;
    }
    items.forEach((item) => {
      const box = el('article', 'card reveal');
      if (item.is_pinned) box.append(el('span', 'badge badge--reviewing', 'سنجاق‌شده'));
      box.append(el('h3', null, item.title));
      box.append(el('p', null, item.content));
      if (item.published_at) box.append(el('div', 'card__meta', formatDate(item.published_at)));
      container.append(box);
    });
  }

  function renderGallery(data) {
    const containers = document.querySelectorAll('[data-render="gallery"]');
    const items = data.gallery || [];
    containers.forEach((container) => {
      clear(container);
      if (items.length === 0) {
        emptyState(container, 'تصویری منتشر نشده است', 'تصاویر مدرسه از پنل مدیریت بارگذاری می‌شوند.');
        return;
      }
      const grid = el('div', 'gallery-grid');
      items.forEach((item) => {
        const card = el('figure', 'gallery-card reveal');
        const image = el('img');
        image.src = item.image_path;
        image.alt = item.title || 'تصویر مدرسه';
        image.loading = 'lazy';
        card.append(image);
        if (item.title) {
          const caption = el('figcaption', 'gallery-card__caption', item.title);
          card.append(caption);
        }
        card.addEventListener('click', () => openLightbox(item));
        grid.append(card);
      });
      container.append(grid);
    });
  }

  function openLightbox(item) {
    let box = document.getElementById('gallery-lightbox');
    if (!box) {
      box = el('div', 'lightbox');
      box.id = 'gallery-lightbox';
      box.hidden = true;
      const close = el('button', 'lightbox__close', 'بستن ✕');
      close.type = 'button';
      close.addEventListener('click', () => {
        box.hidden = true;
      });
      box.addEventListener('click', (event) => {
        if (event.target === box) box.hidden = true;
      });
      box.append(close);
      document.body.append(box);
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') box.hidden = true;
      });
    }
    const image = el('img');
    image.src = item.image_path;
    image.alt = item.title || 'تصویر مدرسه';
    const caption = el('div', 'lightbox__caption', item.title || '');
    box.replaceChildren();
    const close = el('button', 'lightbox__close', 'بستن ✕');
    close.type = 'button';
    close.addEventListener('click', () => {
      box.hidden = true;
    });
    box.append(close, image, caption);
    box.hidden = false;
  }

  function renderFaqs(data) {
    const containers = document.querySelectorAll('[data-render="faqs"]');
    const items = data.faqs || [];
    containers.forEach((container) => {
      clear(container);
      if (items.length === 0) {
        emptyState(container, 'پرسشی ثبت نشده است', 'پرسش‌های متداول از پنل مدیریت اضافه می‌شوند.');
        return;
      }
      const list = el('div', 'faq');
      items.forEach((item) => {
        const box = el('article', 'faq__item');
        box.setAttribute('open-state', 'closed');
        const button = el('button', 'faq__question');
        button.type = 'button';
        button.append(el('span', null, item.question));
        button.append(el('span', null, '+'));
        const answer = el('div', 'faq__answer', item.answer);
        answer.hidden = true;
        button.addEventListener('click', () => {
          const isOpen = box.getAttribute('open-state') === 'open';
          box.setAttribute('open-state', isOpen ? 'closed' : 'open');
          answer.hidden = isOpen;
          button.setAttribute('aria-expanded', String(!isOpen));
        });
        button.setAttribute('aria-expanded', 'false');
        box.append(button, answer);
        list.append(box);
      });
      container.append(list);
    });
  }

  function renderContact(data) {
    const container = document.getElementById('contact-grid');
    const notice = document.getElementById('contact-notice');
    if (!container) return;
    const info = data.school || {};
    const settings = data.settings || {};
    clear(container);

    const items = [
      { label: 'نشانی', value: info.address, icon: '📍', href: info.map_url || '' },
      { label: 'تلفن', value: info.phone ? H.toPersianDigits(info.phone) : '', icon: '☎️', href: info.phone ? `tel:${String(info.phone).replace(/\s/g, '')}` : '' },
      { label: 'ایمیل', value: info.email, icon: '✉️', href: info.email ? `mailto:${info.email}` : '' },
      { label: 'ساعات پاسخ‌گویی', value: info.office_hours, icon: '🕘', href: '' },
    ].filter((item) => item.value);

    if (items.length === 0) {
      emptyState(container, 'اطلاعات تماس منتشر نشده است', settings.contact_notice || 'اطلاعات تماس پس از تأیید منتشر می‌شود.');
      return;
    }

    items.forEach((item) => {
      const box = el('div', 'contact-item');
      box.append(el('div', 'contact-item__icon', item.icon));
      const body = el('div');
      body.append(el('strong', null, item.label));
      if (item.href) {
        const link = el('a', null, item.value);
        link.href = item.href;
        if (item.href.startsWith('http')) {
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
        }
        body.append(link);
      } else {
        body.append(el('span', null, item.value));
      }
      box.append(body);
      container.append(box);
    });

    if (notice && settings.contact_notice) notice.textContent = settings.contact_notice;
  }

  function applySectionVisibility(data) {
    const sections = (data.settings && data.settings.sections) || {};
    const map = {
      '/': ['grades', 'fields', 'staff', 'achievements', 'news', 'gallery', 'faqs', 'contact'],
      '/about': ['grades'],
      '/fields': ['fields'],
      '/grades': ['grades'],
      '/staff': ['staff'],
      '/achievements': ['achievements'],
      '/gallery': ['gallery'],
      '/faq': ['faqs'],
      '/contact': ['contact'],
    };
    const page = document.body.dataset.page || 'home';
    const visible = map[`/${page === 'home' ? '' : page}`] || [];
    document.querySelectorAll('[data-section]').forEach((node) => {
      const key = node.dataset.section;
      if (!(key in sections)) return;
      if (sections[key] === false && visible.includes(key)) node.hidden = true;
      else if (sections[key] === false && !node.classList.contains('page-hero')) node.hidden = true;
    });
  }

  /* ------------------------- صفحه‌های ویژه ------------------------- */

  async function renderNewsPage() {
    const listContainer = document.getElementById('news-page-list');
    if (!listContainer) return;
    const pagination = document.getElementById('news-pagination');
    const params = new URLSearchParams(window.location.search);
    const page = Math.max(Number.parseInt(params.get('page') || '1', 10) || 1, 1);

    listContainer.replaceChildren(el('div', 'skeleton'));
    try {
      const data = await api.get(`/api/news?page=${page}&perPage=9`);
      clear(listContainer);
      if (!data.items || data.items.length === 0) {
        emptyState(listContainer, 'خبری منتشر نشده است', 'به‌زودی اخبار و اطلاعیه‌های مدرسه در این صفحه منتشر می‌شود.');
      } else {
        data.items.forEach((item) => listContainer.append(newsCard(item)));
      }
      if (pagination) {
        clear(pagination);
        if (data.pages > 1) {
          for (let index = 1; index <= data.pages; index += 1) {
            const button = el('button', null, H.toPersianDigits(index));
            button.type = 'button';
            if (index === data.page) button.setAttribute('aria-current', 'true');
            button.addEventListener('click', () => {
              window.location.search = `?page=${index}`;
            });
            pagination.append(button);
          }
        }
      }
    } catch (error) {
      clear(listContainer);
      emptyState(listContainer, 'دریافت اخبار ناموفق بود', error.message);
    }
  }

  async function renderNewsDetail() {
    const container = document.getElementById('news-detail');
    if (!container) return;
    const slug = decodeURIComponent(window.location.pathname.replace('/news/', ''));
    const relatedContainer = document.getElementById('news-related');
    container.replaceChildren(el('div', 'skeleton'));

    try {
      const data = await api.get(`/api/news/${encodeURIComponent(slug)}`);
      const item = data.item;
      clear(container);
      H.setText('#news-title', item.title);
      document.title = `${item.title} | ${state.data ? schoolName(state.data) : 'دبیرستان'}`;
      const meta = el('div', 'news-item__meta');
      meta.append(el('span', null, `انتشار: ${formatDate(item.published_at || item.created_at)}`));
      meta.append(el('span', null, `بازدید: ${formatNumber(item.views)}`));
      container.append(meta);
      if (item.cover_image) {
        const image = el('img');
        image.src = item.cover_image;
        image.alt = item.title;
        image.style.borderRadius = 'var(--radius)';
        image.style.marginBlock = '1rem';
        container.append(image);
      }
      if (item.summary) {
        const lead = el('p', 'notice-box', item.summary);
        container.append(lead);
      }
      String(item.content || '')
        .split('\n')
        .filter((line) => line.trim() !== '')
        .forEach((line) => container.append(el('p', null, line)));

      if (relatedContainer) {
        clear(relatedContainer);
        if (data.related && data.related.length > 0) {
          data.related.forEach((related) => relatedContainer.append(newsCard(related)));
        } else {
          relatedContainer.hidden = true;
        }
      }
    } catch (error) {
      clear(container);
      emptyState(container, 'این خبر یافت نشد', error.message);
    }
  }

  function renderContactPage(data) {
    const container = document.getElementById('contact-grid');
    if (!container) return;
    const info = data.school || {};
    const settings = data.settings || {};
    clear(container);

    const rows = [
      ['نشانی', info.address, info.map_url],
      ['تلفن', info.phone ? H.toPersianDigits(info.phone) : '', info.phone ? `tel:${String(info.phone).replace(/\s/g, '')}` : ''],
      ['موبایل', info.mobile ? H.toPersianDigits(info.mobile) : '', ''],
      ['ایمیل', info.email, info.email ? `mailto:${info.email}` : ''],
      ['نمابر', info.fax ? H.toPersianDigits(info.fax) : '', ''],
      ['ساعات پاسخ‌گویی', info.office_hours, ''],
    ].filter((row) => row[1]);

    if (rows.length === 0) {
      emptyState(
        container,
        'اطلاعات تماس هنوز تأیید نشده است',
        settings.contact_notice || 'پس از تکمیل اطلاعات تماس در پنل مدیریت، این بخش منتشر می‌شود.'
      );
      return;
    }

    const panel = el('div', 'panel');
    const list = el('dl', 'kv');
    rows.forEach(([label, value, href]) => {
      const row = el('div', 'kv__row');
      row.append(el('dt', null, label));
      const dd = el('dd');
      if (href) {
        const link = el('a', null, value);
        link.href = href;
        if (href.startsWith('http')) {
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
        }
        dd.append(link);
      } else {
        dd.textContent = value;
      }
      row.append(dd);
      list.append(row);
    });
    panel.append(list);
    container.append(panel);
  }

  /* ------------------------------ راه‌اندازی ------------------------------ */

  async function init() {
    const page = document.body.dataset.page || 'home';
    const needsSite = page !== 'news-detail';

    try {
      if (needsSite) {
        const data = await loadSiteData();
        renderTopbar(data);
        renderHeader(data, page);

        if (page === 'home') {
          renderHero(data);
          renderAbout(data);
          renderGrades(data);
          renderFields(data);
          renderStaff(data);
          renderAchievements(data);
          renderNews(data);
          renderAnnouncements(data);
          renderGallery(data);
          renderFaqs(data);
          renderContact(data);
          H.setText('#home-about-title', `درباره ${schoolName(data)}`);
        } else if (page === 'about') {
          renderAbout(data);
          renderGrades(data);
          H.setText('#page-title', schoolName(data));
        } else if (page === 'fields') {
          renderFields(data);
        } else if (page === 'grades') {
          renderGrades(data);
        } else if (page === 'staff') {
          renderStaff(data);
        } else if (page === 'achievements') {
          renderAchievements(data);
        } else if (page === 'gallery') {
          renderGallery(data);
        } else if (page === 'faq') {
          renderFaqs(data);
        } else if (page === 'contact') {
          renderContactPage(data);
        } else if (page === 'news') {
          renderAnnouncements(data);
        }

        applySectionVisibility(data);
        if (data.settings) {
          const info = data.school || {};
          if (info.name) document.title = document.title.replace('دبیرستان', info.name);
        }
      } else {
        const data = await loadSiteData();
        renderTopbar(data);
        renderHeader(data, page);
      }
      renderFooter(state.data || {});
      revealOnScroll();
    } catch (error) {
      const main = document.querySelector('main');
      if (main) {
        const alertBox = el('div', 'alert alert--error');
        alertBox.textContent = `دریافت اطلاعات سایت ناموفق بود: ${error.message}`;
        main.prepend(alertBox);
      }
    }

    if (page === 'news') renderNewsPage();
    if (page === 'news-detail') renderNewsDetail();
  }

  window.SwSite = { init, loadSiteData, schoolName, openLightbox };

  document.addEventListener('DOMContentLoaded', init);
})();
