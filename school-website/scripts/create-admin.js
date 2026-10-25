#!/usr/bin/env node
'use strict';

/**
 * ساخت حساب مدیر از خط فرمان (روش پیشنهادی روی هاست).
 *
 * نمونه استفاده:
 *   node scripts/create-admin.js --username admin --name "نام مدیر" --password "یک رمز قوی"
 *   ADMIN_PASSWORD="..." node scripts/create-admin.js --username admin
 *
 * نکته: رمز عبور هرگز در لاگ یا تاریخچه پوسته چاپ نمی‌شود؛ در صورت تمایل از
 * متغیر محیطی ADMIN_PASSWORD استفاده کنید.
 */

const bcrypt = require('bcryptjs');

const { initDb, closeDb } = require('../server/database/db');
const { getDb } = require('../server/database/db');
const { isValidUsername, strongEnoughPassword } = require('../server/utils/validate');
const { seedDefaults } = require('../server/database/seed');

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith('--')) continue;
    const key = token.slice(2);
    const next = argv[index + 1];
    if (next && !next.startsWith('--')) {
      args[key] = next;
      index += 1;
    } else {
      args[key] = true;
    }
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const username = String(args.username || process.env.ADMIN_USERNAME || '').trim();
  const fullName = String(args.name || process.env.ADMIN_NAME || '').trim();
  const password = String(args.password || process.env.ADMIN_PASSWORD || '');
  const role = ['owner', 'admin', 'editor'].includes(String(args.role)) ? String(args.role) : 'owner';

  if (!username || !password) {
    console.error('نام کاربری و رمز عبور الزامی است. نمونه:');
    console.error('  node scripts/create-admin.js --username admin --name "مدیر مدرسه" --password "رمز-قوی"');
    process.exit(1);
  }
  if (!isValidUsername(username)) {
    console.error('نام کاربری باید ۳ تا ۶۰ نویسه لاتین/رقم/نقطه/خط تیره/زیرخط باشد.');
    process.exit(1);
  }
  if (!strongEnoughPassword(password)) {
    console.error('رمز عبور باید حداقل ۱۰ نویسه و شامل حرف و رقم باشد.');
    process.exit(1);
  }

  initDb();
  seedDefaults();
  const db = getDb();
  const existing = db.prepare('SELECT id FROM admins WHERE username = ?').get(username);

  if (existing) {
    db.prepare(
      "UPDATE admins SET password_hash = ?, full_name = COALESCE(NULLIF(?, ''), full_name), role = ?, is_active = 1, must_change_password = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?"
    ).run(bcrypt.hashSync(password, 12), fullName, role, existing.id);
    console.log(`رمز و اطلاعات حساب «${username}» به‌روزرسانی شد.`);
  } else {
    db.prepare(
      'INSERT INTO admins (username, password_hash, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)'
    ).run(username, bcrypt.hashSync(password, 12), fullName, role);
    console.log(`حساب مدیر «${username}» با نقش ${role} ساخته شد.`);
  }
  closeDb();
}

main();
