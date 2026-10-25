#!/usr/bin/env node
'use strict';

/** نگهداری فایل‌های بارگذاری‌شده در مخزن و ساخت پوشه در صورت نبود */

const fs = require('fs');
const path = require('path');

const { publicDir } = require('../server/config');

const target = path.join(publicDir, 'uploads');
fs.mkdirSync(target, { recursive: true });
const keep = path.join(target, '.gitkeep');
if (!fs.existsSync(keep)) fs.writeFileSync(keep, '');
console.log(`پوشه بارگذاری آماده است: ${target}`);
