/**
 * daily-check → Googleカレンダー「bosh」に自動予約したテニスを登録する Web アプリ
 * 設定：スクリプトのプロパティ TOKEN に、GitHub Secrets の CALENDAR_HOOK_TOKEN と同じ値を入れる
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
  const at = (d, hm) => new Date(d + 'T' + (hm.length === 4 ? '0' + hm : hm) + ':00+09:00');

  // テニスの予定：10分前と前日20:00に通知
  const start = at(p.date, p.start), end = at(p.date, p.end);
  const ev = cal.createEvent(p.title, start, end, {location: p.location, description: p.description});
  ev.removeAllReminders();
  ev.addPopupReminder(10);
  ev.addPopupReminder(Math.round((start - at(p.date, '20:00')) / 60000) + 24 * 60);

  // 無料キャンセル期限：期限日の9:00に予定を作り、10分前と前日9:00に通知
  if (p.cancel_deadline) {
    const s = at(p.cancel_deadline, '09:00');
    const c = cal.createEvent('【無料キャンセル期限】' + p.title, s, new Date(s.getTime() + 30 * 60000), {
      description: '今日までなら、予約システムの「予約内容の確認・取消」から無料でキャンセルできます。\n' +
                   '明日以降は電話か窓口での取消になり、規定の利用料がかかります。\n\n利用日：' +
                   p.date + ' ' + p.start + '～' + p.end + '\nhttps://www2.pf489.com/Toshima/WebR/'});
    c.removeAllReminders();
    c.addPopupReminder(10);
    c.addPopupReminder(24 * 60);
  }
  return ContentService.createTextOutput('ok');
}
