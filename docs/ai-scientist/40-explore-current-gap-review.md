# EXPLORE: mevcut kaynak incelemesi ve açık uygulama

2026-09-27 — Astra/high incelemesi ve root CPU gözlemi, runtime 0.32.0.

## Bağlayıcı kapsam

M0 brief'i §3(j), Director durum makinesiyle birlikte plato/EXPLORE'u açıkça
ister. Spec §3.3.2: 25 ardışık KEEP olmayan deneyden sonra 10 S2 deneyi,
zorunlu algoritma ailesi değişimi ve KEEP çıkmazsa END. Numaralı plato testi
M4.6 altında olsa da brief'in M0 uygulama şartı korunur. Mevcut M0.9'un
20 deneylik sentetik kabulü bu davranışı kanıtlamaz.

## Doğrulanan mevcut eksik

`lab/director/loop.py` satır 426–434, plato koşulunda herhangi bir yeni
öneri çağırmadan `explore_family_unverified` döndürüyor. `lab/llm/router.py`
EXPLORE için S2 seçebiliyor; eksik olan aile doğrulaması ve bölüm yönetimi.
Bu kapıyı kaldırmak tek başına doğru bir uygulama oluşturmaz.

[CPU gözlem kaydı](review-evidence/explore-032-gap-probe-result.json):
session **50380 / exit 0**, production `DirectorLoop.run()` için bellekte
25 tamamlanmış/non-KEEP deney durumu enjekte edildi. Sonuç
`explore_family_unverified`, sonraki ordinal 26, provider çağrısı 0 ve yeni
checkpoint 0. Bu 25 deney gerçekten yürütülmedi; kalibrasyon, depolama,
kimlik ve holdout hazırlığı fixture ile değiştirildi. PostgreSQL, Docker,
model/GPU veya AOS başlatılmadı. Exit 0 eksikliğin yeniden üretildiğini
gösterir; EXPLORE kabulü değildir. Yedi kaynak dosyasının hash'i sabit kaldı;
[sürücü](review-evidence/explore-032-gap-probe.py) aynı kayda bağlıdır.

Kaynakta ayrıca `explore_family` alanına algoritma ailesi yerine `move_type`
yazılıyor; `explore_proposals` KEEP sonrasında bölüm bazında sıfırlanmıyor.
On deneme sonrasına ait açık `explore_exhausted → HOLDOUT_CHECK → REPORT → END`
geçişi yok. **Mevcut toplam 35 öneri tavanı**, ilk 25+10 koşusunda zaten döngüyü
keser; 36. gerçek provider çağrısı veya ikinci plato bu kontrolde gözlenmedi.
Tavanı sessizce yükseltmek bu incelemenin veya sonraki dilimin parçası değildir.

## Sonraki tutarlı uygulama dilimi

- Algoritma ailesini hareket tipinden ayır. Kaynak hash'ine bağlı, sürümlü
  güvenilir sınıflandırma desteklediği detector→score davranışlarını açıkça
  tanımlasın; yalnız import, yorum veya model beyanını kanıt saymasın.
  Desteklenmeyen/çok anlamlı kaynak aile değişimi olarak kabul edilmesin.
  Genel Python programları için anlamsal eşdeğerlik kanıtı iddia edilmesin.
- EXPLORE bölüm kimliği, başlangıç şampiyonu/ailesi, seçilen farklı hedef aile
  ve deneme sayısı provider çağrısından önce kalıcı intent'e yazılsın.
  Aile yönergesi fake/local context ve prompt hash'ine dahil edilsin.
- `runner.register_proposal_before_execution` ölçüm admission'ından önce
  hedef aileyi doğrulasın. Uygulanamayan yönerge typed abandon üretsin;
  Thompson hareket seçimi, RNG ve sonuç çözümleme kuralları korunsun.
- KEEP/KEEP_SIMPLER normal terfi ve holdout yolundan LOOP'a dönsün;
  EXPLORE bölüm sayacı sıfırlansın. On sonuçsuz denemeden sonra açık terminal
  geçiş kaydedilsin. Rollback şampiyonun aile kimliğini de geri yüklesin.
- Restart aynı intent/hedef/sayacı sürdürsün; terminal commit ile durum
  checkpoint'i arasındaki kesinti bir denemeyi ikinci kez saymasın.

## Gerekli kanıt ve sınır

İlk fixture proof gerçek döngüyle 25 non-KEEP → 10 S2 → END akışını, yanlış
aile/unused-import/unknown kaynakların ölçüm kuyruğuna girememesini ve
terminal rapor yolunu doğrulamalı. KEEP reset'i ve ikinci bölüm, mevcut run
bütçelerini aşmadan ayrı geçerli durum fixture'ıyla sınanmalı; böyle bir
fixture ikinci uzun koşunun uçtan uca kanıtı sayılmaz. Provider altyapı hatası
hedefi veya RNG'yi yeniden seçmemeli. Kalıcı SQL/işlem kesintisi kanıtı ve
yerel modelin yönergeyi uygulaması ayrı tutulmalı.

Baseline/0025 sahiplik entegrasyonu mevcut önceliktir. Bu inceleme yeni bir
runtime sürümü, uygulama onayı veya kabul maddesi kapatmaz.
