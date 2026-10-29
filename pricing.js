// Interview Hub: lavozim narxlari va paket formulalari (UZS, so'm)
// Narxlar GIPOTEZA: pilotdan keyin 10-15 ishtirokchi so'rovi bilan tekshiriladi.

const POSITIONS = [
  // Narx tartibi: yuqoridan pastga kamayadi
  { id: 'accounting',  name: 'Accounting',         price: 900000, tier: 'A', available: true },
  { id: 'updater',     name: 'Updater',            price: 800000, tier: 'A', available: true },
  { id: 'safety',      name: 'Safety',             price: 700000, tier: 'B', available: true },
  { id: 'dispatch_bt', name: 'Dispatch box truck', price: 650000, tier: 'B', available: true },
  { id: 'fleet',       name: 'Fleet',              price: 600000, tier: 'B', available: true },
  { id: 'eld',         name: 'ELD',                price: 550000, tier: 'C', available: true },
  { id: 'sales',       name: 'Sales',              price: 500000, tier: 'C', available: true },
  { id: 'recruiting',  name: 'Recruiting',         price: 450000, tier: 'C', available: true },
];

// Tanlangan lavozimlar soniga qarab chegirma
const DISCOUNT = { 1: 0, 2: 0.15, 3: 0.25 };
const MAX_POSITIONS = 3;
const ROUND_STEP = 500; // so'm

// Mijoz taqsimoti (o'rtacha chek hisobi uchun, taxminiy)
const MIX = { 1: 0.5, 2: 0.3, 3: 0.2 };

function roundStep(x) { return Math.round(x / ROUND_STEP) * ROUND_STEP; }

function quote(ids) {
  const unique = [...new Set(ids)];
  if (unique.length === 0 || unique.length > MAX_POSITIONS) {
    throw new Error(`1 dan ${MAX_POSITIONS} gacha lavozim tanlang`);
  }
  const picked = unique.map(id => {
    const p = POSITIONS.find(x => x.id === id);
    if (!p) throw new Error(`Noma'lum lavozim: ${id}`);
    if (!p.available) throw new Error(`${p.name} uchun hozir ochiq vakansiya yo'q`);
    return p;
  });
  const subtotal = picked.reduce((s, p) => s + p.price, 0);
  const discount = DISCOUNT[picked.length];
  const total = roundStep(subtotal * (1 - discount));
  return { positions: picked.map(p => p.name), count: picked.length, subtotal, discount, total, saved: subtotal - total };
}

// n ta lavozimdan iborat barcha kombinatsiyalar bo'yicha o'rtacha narx
function avgPackagePrice(n) {
  const pool = POSITIONS.filter(p => p.available);
  let sum = 0, cnt = 0;
  const rec = (start, chosen) => {
    if (chosen.length === n) { sum += quote(chosen).total; cnt++; return; }
    for (let i = start; i < pool.length; i++) rec(i + 1, [...chosen, pool[i].id]);
  };
  rec(0, []);
  return sum / cnt;
}

function averageCheck(mix = MIX) {
  return Object.entries(mix).reduce((s, [n, share]) => s + share * avgPackagePrice(+n), 0);
}

function revenue(clients, mix = MIX) { return Math.round(clients * averageCheck(mix)); }

const API = { POSITIONS, DISCOUNT, MAX_POSITIONS, MIX, quote, avgPackagePrice, averageCheck, revenue };
if (typeof module !== 'undefined' && module.exports) module.exports = API; // Node (bot)
else window.PRICING = API;                                                  // Brauzer (mini app)
