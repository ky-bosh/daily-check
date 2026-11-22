/**
 * daily-check → Googleカレンダー「bosh」に予約を登録する Web アプリ
 * 1. スクリプトのプロパティに TOKEN を設定（Secrets の CALENDAR_HOOK_TOKEN と同じ値）
 * 2. デプロイ → 新しいデプロイ → 種類「ウェブアプリ」、実行ユーザー「自分」、アクセス「全員」
 */
const CALENDAR_NAME = 'bosh';   // このカレンダー以外には書き込まない

function doPost(e) {
  const p = JSON.parse(e.postData.contents);
  if (p.token !== PropertiesService.getScriptProperties().getProperty('TOKEN')) {
    return ContentService.createTextOutput('forbidden');
  }
  const cals = CalendarApp.getCalendarsByName(CALENDAR_NAME);
  if (cals.length !== 1) return ContentService.createTextOutput('calendar not found');
  const cal = cals[0];

  // テニスの予定：10分前と前日20時ごろに通知
  const start = new Date(p.date + 'T' + p.start.padStart(5, '0') + ':00+09:00');
  const end = new Date(p.date + 'T' + p.end.padStart(5, '0') + ':00+09:00');
  const ev = cal.createEvent(p.title, start, end, {location: p.location, description: p.description});
  ev.removeAllReminders();
  ev.addPopupReminder(10);
  ev.addPopupReminder(Math.round((start - new Date(p.date + 'T20:00:00+09:00')) / 60000) + 24 * 60);

  // 窓口支払い期限（終日の予定）：前日9:00と前日21:00に通知（終日予定は当日朝の通知を設定できない）
  if (p.pay_due) {
    const due = new Date(p.pay_due + 'T00:00:00+09:00');
    const pay = cal.createAllDayEvent('【支払い期限】' + p.title + '（窓口）', due,
      {description: '自動予約したテニスの使用料を窓口で支払う期限（仮：予約から7日。施設に要確認）\n利用日：' + p.date + ' ' + p.start + '～' + p.end});
    pay.removeAllReminders();
    pay.addPopupReminder(15 * 60);   // 前日 9:00
    pay.addPopupReminder(3 * 60);    // 前日 21:00
  }
  return ContentService.createTextOutput('ok');
}
