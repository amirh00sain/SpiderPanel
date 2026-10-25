#!/usr/bin/env node
'use strict';

/**
 * بازیابی دیتابیس از فایل پشتیبان.
 * نمونه: node scripts/restore.js --file storage/backups/school-2026-01-01T00-00-00-000Z.db
 *
 * پیش از جای‌گزینی، از دیتابیس فعلی یک پشتیبان امن گرفته می‌شود.
 */

const fs = require('fs');
const path = require('path');

const config = require('../server/config');

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--file') {
      args.file = argv[index + 1];
      index += 1;
    }
  }
  return args;
}

function main() {
  const { file } = parseArgs(process.argv.slice(2));
  if (!file) {
    console.error('مسیر فایل پشتیبان را با --file مشخص کنید.');
    process.exit(1);
  }
  const source = path.resolve(file);
  if (!fs.existsSync(source)) {
    console.error(`فایل پشتیبان یافت نشد: ${source}`);
    process.exit(1);
  }

  const header = fs.readFileSync(source).subarray(0, 16).toString('utf8');
  if (!header.startsWith('SQLite format 3')) {
    console.error('فایل انتخابی یک دیتابیس SQLite معتبر نیست.');
    process.exit(1);
  }

  fs.mkdirSync(config.backupDir, { recursive: true });
  if (fs.existsSync(config.dbPath)) {
    const safety = path.join(
      config.backupDir,
      `before-restore-${new Date().toISOString().replace(/[:.]/g, '-')}.db`
    );
    fs.copyFileSync(config.dbPath, safety);
    console.log(`پشتیبان ایمن از دیتابیس فعلی: ${safety}`);
  }

  for (const suffix of ['-wal', '-shm']) {
    const sidecar = `${config.dbPath}${suffix}`;
    if (fs.existsSync(sidecar)) fs.unlinkSync(sidecar);
  }

  fs.copyFileSync(source, config.dbPath);
  console.log(`دیتابیس از ${source} بازیابی شد. سرور را دوباره اجرا کنید.`);
}

main();
