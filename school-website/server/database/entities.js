'use strict';

/**
 * فهرست موجودیت‌های محتوایی که مدیر از پنل مدیریت کنترل می‌کند.
 * نام جدول و فیلدهای مجاز فقط از همین فهرست خوانده می‌شود؛ هیچ ورودی کاربر
 * هرگز به عنوان نام جدول یا ستون در SQL استفاده نمی‌شود.
 */

const entities = {
  grades: {
    title: 'پایه‌های تحصیلی',
    table: 'grades',
    description: 'پایه‌های دهم، یازدهم و دوازدهم',
    searchFields: ['title', 'description'],
    orderBy: 'sort_order ASC, grade_number ASC, id ASC',
    fields: [
      { name: 'title', label: 'عنوان پایه', type: 'text', required: true, maxLength: 120 },
      { name: 'grade_number', label: 'شماره پایه', type: 'number', min: 7, max: 12, nullable: true },
      { name: 'description', label: 'توضیحات', type: 'textarea', maxLength: 3000 },
      { name: 'is_active', label: 'نمایش در سایت', type: 'boolean', default: 1 },
      { name: 'sort_order', label: 'ترتیب نمایش', type: 'number', default: 0 },
    ],
    columns: ['id', 'title', 'grade_number', 'is_active', 'sort_order'],
  },

  'study-fields': {
    title: 'رشته‌های تحصیلی',
    table: 'study_fields',
    description: 'رشته‌های قابل انتخاب در پیش‌ثبت‌نام',
    searchFields: ['title', 'description'],
    orderBy: 'sort_order ASC, id ASC',
    fields: [
      { name: 'title', label: 'نام رشته', type: 'text', required: true, maxLength: 120 },
      { name: 'description', label: 'توضیحات', type: 'textarea', maxLength: 3000 },
      { name: 'icon', label: 'آیکون (نام کوتاه یا آدرس تصویر)', type: 'text', maxLength: 200 },
      { name: 'color', label: 'رنگ کارت', type: 'color' },
      { name: 'is_active', label: 'نمایش در سایت', type: 'boolean', default: 1 },
      { name: 'sort_order', label: 'ترتیب نمایش', type: 'number', default: 0 },
    ],
    columns: ['id', 'title', 'color', 'is_active', 'sort_order'],
  },

  staff: {
    title: 'کادر مدیریتی و دبیران',
    table: 'staff',
    description: 'اعضای کادر مدرسه',
    searchFields: ['full_name', 'role_title'],
    orderBy: 'sort_order ASC, id ASC',
    fields: [
      { name: 'full_name', label: 'نام و نام خانوادگی', type: 'text', required: true, maxLength: 120 },
      { name: 'role_title', label: 'سمت', type: 'text', maxLength: 120 },
      { name: 'bio', label: 'معرفی کوتاه', type: 'textarea', maxLength: 2000 },
      { name: 'photo_path', label: 'تصویر', type: 'image' },
      { name: 'email', label: 'ایمیل', type: 'email', maxLength: 160 },
      { name: 'phone', label: 'شماره تماس', type: 'tel', maxLength: 40 },
      { name: 'is_active', label: 'نمایش در سایت', type: 'boolean', default: 1 },
      { name: 'sort_order', label: 'ترتیب نمایش', type: 'number', default: 0 },
    ],
    columns: ['id', 'full_name', 'role_title', 'is_active', 'sort_order'],
  },

  news: {
    title: 'اخبار و اطلاعیه‌ها',
    table: 'news',
    description: 'خبرهای مدرسه با صفحه جزئیات',
    searchFields: ['title', 'summary', 'content'],
    orderBy: 'COALESCE(published_at, created_at) DESC, id DESC',
    slugFrom: 'title',
    fields: [
      { name: 'title', label: 'عنوان خبر', type: 'text', required: true, maxLength: 200 },
      { name: 'summary', label: 'خلاصه', type: 'textarea', maxLength: 500 },
      { name: 'content', label: 'متن کامل', type: 'textarea', required: true, maxLength: 20000 },
      { name: 'cover_image', label: 'تصویر شاخص', type: 'image' },
      { name: 'is_published', label: 'منتشر شده', type: 'boolean', default: 0, publishField: true },
      { name: 'published_at', label: 'زمان انتشار', type: 'datetime' },
    ],
    columns: ['id', 'title', 'is_published', 'published_at', 'views'],
  },

  announcements: {
    title: 'اطلاعیه‌های کوتاه',
    table: 'announcements',
    description: 'اطلاعیه‌های سریع نوار بالای سایت',
    searchFields: ['title', 'content'],
    orderBy: 'is_pinned DESC, COALESCE(published_at, created_at) DESC, id DESC',
    fields: [
      { name: 'title', label: 'عنوان اطلاعیه', type: 'text', required: true, maxLength: 200 },
      { name: 'content', label: 'متن اطلاعیه', type: 'textarea', required: true, maxLength: 5000 },
      { name: 'is_pinned', label: 'سنجاق شده', type: 'boolean', default: 0 },
      { name: 'is_published', label: 'منتشر شده', type: 'boolean', default: 0, publishField: true },
      { name: 'published_at', label: 'زمان انتشار', type: 'datetime' },
      { name: 'expires_at', label: 'پایان اعتبار', type: 'datetime' },
    ],
    columns: ['id', 'title', 'is_pinned', 'is_published', 'published_at'],
  },

  achievements: {
    title: 'افتخارات و دستاوردها',
    table: 'achievements',
    description: 'افتخارات دانش‌آموزان و مدرسه',
    searchFields: ['title', 'description', 'year'],
    orderBy: 'sort_order ASC, id DESC',
    fields: [
      { name: 'title', label: 'عنوان افتخار', type: 'text', required: true, maxLength: 200 },
      { name: 'description', label: 'توضیحات', type: 'textarea', maxLength: 3000 },
      { name: 'year', label: 'سال / دوره', type: 'text', maxLength: 40 },
      { name: 'image_path', label: 'تصویر', type: 'image' },
      { name: 'is_active', label: 'نمایش در سایت', type: 'boolean', default: 1 },
      { name: 'sort_order', label: 'ترتیب نمایش', type: 'number', default: 0 },
    ],
    columns: ['id', 'title', 'year', 'is_active', 'sort_order'],
  },

  gallery: {
    title: 'گالری تصاویر',
    table: 'gallery',
    description: 'تصاویر مدرسه',
    searchFields: ['title', 'category'],
    orderBy: 'sort_order ASC, id DESC',
    fields: [
      { name: 'title', label: 'عنوان تصویر', type: 'text', maxLength: 200 },
      { name: 'image_path', label: 'تصویر', type: 'image', required: true },
      { name: 'category', label: 'دسته‌بندی', type: 'text', maxLength: 80 },
      { name: 'is_active', label: 'نمایش در سایت', type: 'boolean', default: 1 },
      { name: 'sort_order', label: 'ترتیب نمایش', type: 'number', default: 0 },
    ],
    columns: ['id', 'title', 'category', 'is_active', 'sort_order'],
  },

  faqs: {
    title: 'پرسش‌های متداول',
    table: 'faqs',
    description: 'پرسش و پاسخ‌های پرتکرار',
    searchFields: ['question', 'answer'],
    orderBy: 'sort_order ASC, id ASC',
    fields: [
      { name: 'question', label: 'پرسش', type: 'text', required: true, maxLength: 300 },
      { name: 'answer', label: 'پاسخ', type: 'textarea', required: true, maxLength: 5000 },
      { name: 'is_active', label: 'نمایش در سایت', type: 'boolean', default: 1 },
      { name: 'sort_order', label: 'ترتیب نمایش', type: 'number', default: 0 },
    ],
    columns: ['id', 'question', 'is_active', 'sort_order'],
  },

  admins: {
    title: 'مدیران پنل',
    table: 'admins',
    description: 'حساب‌های دسترسی به پنل مدیریت',
    searchFields: ['username', 'full_name'],
    orderBy: 'id ASC',
    protectedFields: ['password_hash'],
    fields: [
      { name: 'username', label: 'نام کاربری', type: 'text', required: true, maxLength: 60, pattern: 'username' },
      { name: 'full_name', label: 'نام و نام خانوادگی', type: 'text', maxLength: 120 },
      {
        name: 'password',
        label: 'رمز عبور',
        type: 'password',
        mapsTo: 'password_hash',
        minLength: 10,
        maxLength: 200,
        hint: 'حداقل ۱۰ نویسه شامل حرف و رقم. هنگام ویرایش، خالی گذاشتن یعنی تغییر نده.',
      },
      {
        name: 'role',
        label: 'نقش',
        type: 'select',
        options: [
          { value: 'owner', label: 'مدیر ارشد' },
          { value: 'admin', label: 'مدیر' },
          { value: 'editor', label: 'ویرایشگر محتوا' },
        ],
        default: 'admin',
      },
      { name: 'is_active', label: 'حساب فعال', type: 'boolean', default: 1 },
    ],
    columns: ['id', 'username', 'full_name', 'role', 'is_active', 'last_login_at'],
  },
};

function getEntity(name) {
  return entities[name] || null;
}

function listEntities() {
  return Object.keys(entities).map((key) => ({
    key,
    title: entities[key].title,
    description: entities[key].description,
  }));
}

/** توضیحات فرم‌ها برای پنل مدیریت (بدون هیچ مقدار حساس) */
function getMeta(name) {
  const entity = getEntity(name);
  if (!entity) return null;
  return {
    key: name,
    title: entity.title,
    description: entity.description,
    columns: entity.columns,
    orderBy: entity.orderBy,
    hasSortOrder: entity.fields.some((field) => field.name === 'sort_order'),
    fields: entity.fields.map((field) => ({
      name: field.name,
      label: field.label,
      type: field.type,
      required: Boolean(field.required),
      maxLength: field.maxLength || null,
      minLength: field.minLength || null,
      options: field.options || null,
      hint: field.hint || null,
      default: field.default === undefined ? null : field.default,
    })),
  };
}

module.exports = { entities, getEntity, getMeta, listEntities };
