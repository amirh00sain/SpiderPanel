#!/usr/bin/env node
'use strict';

/** درج داده‌های ساختاری اولیه (پایه‌ها، رشته‌ها، پرسش‌های متداول، تنظیمات) */

const { initDb, closeDb } = require('../server/database/db');
const { seedDefaults } = require('../server/database/seed');

initDb();
seedDefaults();
console.log('داده‌های اولیه بررسی و در صورت نیاز درج شد.');
closeDb();
