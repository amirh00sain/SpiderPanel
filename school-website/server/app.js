'use strict';

/**
 * ساخت و راه‌اندازی برنامه Express.
 */

const fs = require('fs');
const path = require('path');

const express = require('express');
const helmet = require('helmet');
const cookieParser = require('cookie-parser');

const config = require('./config');
const logger = require('./utils/logger');
const { initDb, getDb } = require('./database/db');
const { seedDefaults } = require('./database/seed');
const { attachSession, purgeExpiredSessions } = require('./middleware/session');
const { ensureCsrfToken, verifyCsrfToken } = require('./middleware/csrf');
const { rateLimit } = require('./middleware/rateLimit');
const { notFoundHandler, errorHandler } = require('./middleware/errors');
const { clientIp, hashIp } = require('./utils/http');
const otpService = require('./services/otp');

const publicRoutes = require('./routes/public');
const authRoutes = require('./routes/auth');
const adminRoutes = require('./routes/admin');
const adminCrudRoutes = require('./routes/adminCrud');
const setupRoutes = require('./routes/setup');
const { requireAdmin } = require('./middleware/session');

// صفحه‌های عمومی سایت (نشانی تمیز → فایل HTML)
const PUBLIC_PAGES = {
  '/': 'index.html',
  '/about': 'pages/about.html',
  '/grades': 'pages/grades.html',
  '/fields': 'pages/fields.html',
  '/news': 'pages/news.html',
  '/staff': 'pages/staff.html',
  '/gallery': 'pages/gallery.html',
  '/achievements': 'pages/achievements.html',
  '/faq': 'pages/faq.html',
  '/contact': 'pages/contact.html',
  '/pre-registration': 'pages/pre-registration.html',
  '/track': 'pages/track.html',
  '/login': 'pages/login.html',
  '/my-requests': 'pages/my-requests.html',
};

function recordPageView(req, route) {
  try {
    getDb()
      .prepare('INSERT INTO page_views (path, ip_hash) VALUES (?, ?)')
      .run(route, hashIp(clientIp(req)));
  } catch (error) {
    logger.warn(`ثبت بازدید ناموفق بود: ${error.message}`);
  }
}

function securityMiddleware() {
  const directives = {
    defaultSrc: ["'self'"],
    scriptSrc: ["'self'"],
    // مقدارهای رنگ در پنل مدیریت به صورت style درون‌خطی تنظیم می‌شوند
    styleSrc: ["'self'", "'unsafe-inline'"],
    imgSrc: ["'self'", 'data:', 'blob:'],
    fontSrc: ["'self'", 'data:'],
    connectSrc: ["'self'"],
    objectSrc: ["'none'"],
    baseUri: ["'self'"],
    formAction: ["'self'"],
    frameAncestors: ["'self'"],
    // در محیط عملیاتی (HTTPS) درخواست‌های ناامن به https ارتقا داده می‌شوند؛ در اجرای محلی روی http
    // همین دستور باعث خرابی فایل‌های استاتیک می‌شد، پس فقط در production فعال می‌ماند.
    upgradeInsecureRequests: config.isProduction ? [] : null,
  };

  return helmet({
    contentSecurityPolicy: { directives },
    // در اجرای محلی بدون HTTPS نباید HSTS یا ارتقای اجباری به https اعمال شود.
    strictTransportSecurity: config.isProduction ? undefined : false,
    crossOriginEmbedderPolicy: false,
    referrerPolicy: { policy: 'strict-origin-when-cross-origin' },
  });
}

function createApp() {
  initDb();
  seedDefaults();

  const app = express();
  app.disable('x-powered-by');
  if (config.trustProxy) app.set('trust proxy', 1);

  app.use(securityMiddleware());
  app.use(cookieParser());
  app.use(express.json({ limit: '200kb' }));
  app.use(express.urlencoded({ extended: false, limit: '200kb' }));
  app.use(attachSession);
  app.use(ensureCsrfToken);

  // فایل‌های عمومی سایت
  app.use(
    express.static(config.publicDir, {
      index: false,
      dotfiles: 'ignore',
      setHeaders(res, filePath) {
        if (filePath.includes(`${path.sep}assets${path.sep}`)) {
          res.setHeader('cache-control', 'public, max-age=3600');
        }
      },
    })
  );
  // اگر مسیر بارگذاری بیرون پوشه عمومی تنظیم شده باشد، همین‌جا سرو می‌شود.
  app.use('/uploads', express.static(config.uploadDir, { index: false, dotfiles: 'ignore', maxAge: '1h' }));

  app.get('/favicon.ico', (req, res) => {
    res.type('image/svg+xml').sendFile('favicon.svg', { root: config.publicDir });
  });

  // صفحه‌های عمومی
  for (const [route, file] of Object.entries(PUBLIC_PAGES)) {
    app.get(route, (req, res) => {
      recordPageView(req, route);
      res.sendFile(path.join(config.publicDir, file));
    });
  }

  app.get('/news/:slug', (req, res) => {
    recordPageView(req, '/news/:slug');
    res.sendFile(path.join(config.publicDir, 'pages/news-detail.html'));
  });

  // صفحه‌های پنل مدیریت (فقط برای مدیر وارد‌شده)
  function sendAdminPage(page) {
    return (req, res, next) => {
      if (!/^[a-z0-9-]+$/.test(page)) return next();
      const file = path.join(config.publicDir, 'admin', `${page}.html`);
      if (!fs.existsSync(file)) return next();
      return res.sendFile(file);
    };
  }
  function requireAdminPage(req, res, next) {
    if (!req.auth || req.auth.type !== 'admin') return res.redirect('/admin/login');
    return next();
  }

  app.get('/admin', requireAdminPage, sendAdminPage('index'));
  app.get('/admin/login', sendAdminPage('login'));
  // صفحه نصب اولیه بدون نیاز به ورود در دسترس است و خودش پس از ساخت اولین مدیر بسته می‌شود.
  app.get('/admin/setup', sendAdminPage('setup'));
  app.get('/admin/:page', requireAdminPage, (req, res, next) => sendAdminPage(req.params.page)(req, res, next));

  // API
  const apiLimiter = rateLimit({
    name: 'api',
    windowMs: 60 * 1000,
    max: config.rateLimits.apiPerMinute,
    message: 'تعداد درخواست‌ها زیاد است. کمی بعد تلاش کنید.',
  });

  app.use('/api', apiLimiter, verifyCsrfToken);
  app.use('/api/setup', setupRoutes);
  app.use('/api', authRoutes);
  app.use('/api', publicRoutes);
  app.use('/api/admin', requireAdmin, adminRoutes);
  app.use('/api/admin', requireAdmin, adminCrudRoutes);

  app.use(notFoundHandler);
  app.use(errorHandler);

  // پاک‌سازی دوره‌ای داده‌های منقضی
  const maintenance = setInterval(() => {
    try {
      purgeExpiredSessions();
      otpService.purgeOldRequests();
    } catch (error) {
      logger.warn(`نگهداری دوره‌ای ناموفق بود: ${error.message}`);
    }
  }, 60 * 60 * 1000);
  if (maintenance.unref) maintenance.unref();

  return app;
}

function start() {
  const app = createApp();
  const server = app.listen(config.port, config.host, () => {
    logger.info(`سایت روی http://${config.host}:${config.port} در حال اجراست (${config.env}).`);
    logger.info(`دیتابیس: ${config.dbPath}`);
    if (config.sessionSecretGenerated) {
      logger.warn('SESSION_SECRET تنظیم نشده بود؛ برای این اجرا یک کلید موقت ساخته شد. برای محیط عملیاتی آن را در .env تعیین کنید.');
    }
  });

  const shutdown = (signal) => {
    logger.info(`دریافت ${signal}؛ بستن سرور...`);
    server.close(() => process.exit(0));
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));

  return server;
}

module.exports = { createApp, start, PUBLIC_PAGES };

if (require.main === module) {
  start();
}
