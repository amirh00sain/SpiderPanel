#!/usr/bin/env node
'use strict';

/** ساخت/به‌روزرسانی ساختار دیتابیس (اجرای migration‌ها) */

const { initDb, closeDb } = require('../server/database/db');
const { seedDefaults } = require('../server/database/seed');
const config = require('../server/config');

initDb();
seedDefaults();
console.log(`ساختار دیتابیس آماده است: ${config.dbPath}`);
closeDb();
