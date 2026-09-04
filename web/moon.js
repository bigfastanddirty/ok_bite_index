function getMoonPhase() {
  const date = new Date();
  let year = date.getUTCFullYear();
  let month = date.getUTCMonth() + 1;
  const day = date.getUTCDate() + (date.getUTCHours() / 24) + (date.getUTCMinutes() / 1440);

  if (month < 3) {
    year--;
    month += 12;
  }

  const a = Math.floor(year / 100);
  const b = 2 - a + Math.floor(a / 4);
  const jd = Math.floor(365.25 * (year + 4716)) + Math.floor(30.6001 * (month + 1)) + day + b - 1524.5;
  
  // Days since known base new moon (JD 2451549.5)
  const daysSinceNew = jd - 2451549.5;
  const synodicPeriod = 29.53058867;
  const phase = (daysSinceNew / synodicPeriod) - Math.floor(daysSinceNew / synodicPeriod);
  const age = phase * synodicPeriod;

  // Exact 8-phase breakdown with solunar bite weightings
  if (age < 1.84) return { name: 'New Moon (Peak)', icon: '🌑', score: 25 };
  if (age < 5.53) return { name: 'Waxing Crescent', icon: '🌒', score: 14 };
  if (age < 9.22) return { name: 'First Quarter', icon: '🌓', score: 18 };
  if (age < 12.91) return { name: 'Waxing Gibbous', icon: '🌔', score: 16 };
  if (age < 16.61) return { name: 'Full Moon (Peak)', icon: '🌕', score: 25 };
  if (age < 20.30) return { name: 'Waning Gibbous', icon: '🌖', score: 16 };
  if (age < 23.99) return { name: 'Last Quarter', icon: '🌗', score: 18 };
  if (age < 27.68) return { name: 'Waning Crescent', icon: '🌘', score: 14 };
  return { name: 'New Moon (Peak)', icon: '🌑', score: 25 };
}
