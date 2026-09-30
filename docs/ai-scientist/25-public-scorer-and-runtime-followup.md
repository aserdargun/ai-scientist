# Public Scorer ve runtime takip incelemesi

Başlangıç: `7d3fd22633ae280537f88924332563a09ae4d357` / harness 0.24.0.
Bu kayıt devam eden uygulamanın yeni gerçek bulgularını tutar.

## Gerçek public sandbox/Scorer denemesi

`data/runtime/parallel-m0/public-execution` ayrı worktree'si aynı commit ve
harness hash'inden oluşturuldu. Dört DB rolü yalnız daha önce public kayıt
testi için oluşturulan `swapp_lab_m0_public_suite_3ddaa2d8` veritabanını
gösterir. Scorer supervisor kendi PROJECT_ROOT varsayılanlarını kullandığı
için tüm kaynak kopyası ayrıdır; sadece Director DSN değiştirilmemiştir.
Ana DB veya AOS değiştirilmedi. Giriş, hash'i doğrulanmış 27 görevlik gerçek
public v2 manifestidir; sentetik veri veya LLM kullanılmadı.

Robust-z seed 0, gerçek Docker fit/score/determinism/causality/hardcoding
guard'ları ve ayrı systemd Scorer worker yolunda **session 36524 / exit 1**,
**40,070 saniye** verdi. **1/27 görev tamamlandı**. İlk CARE PDM görevi
`care-a-000` teknik skor yazımını geçti (0.0); ikinci görev `care-a-010`
worker'da `retrying` oldu ve test durdu. Tekrar çalıştırılarak hata
örtülmedi. Test yalnız kendine ait private run satırını temizledi; public
kayıtlar ve ana veritabanının koşuları korundu. İmaj/kaynak hash'i sabittir.

Kanıt: `review-evidence/public-sandbox-scorer-024-review.json`,
`public-sandbox-scorer-024-command.json` ve
`review_public_sandbox_scorer.py`. Bu deneme üç algoritma × üç seed
kalibrasyonu, tüm Director döngüsü veya gerçek model kabulü değildir.

## CARE zaman/maske anlamı engeli

Sonraki salt okunur Scorer kayıt kontrolü altı PDM görevinde **bütün eval
satırlarının maskeli** olduğunu buldu: CARE A 010, 022, 045, 068, 073, 084.
Healthy exposure sıfırdır; metrik bu girdiyi reddetmelidir. Ayrıca 12 PDM
görevinin 11'inde maskeden sonra pozitif örnek kalmaz. İlk görevin teknik
0.0 sonucu bu veri anlamının doğru olduğu kanıtı değildir.

`public-scorer-exposure-024-review.json` 27 görevin kayıtlı durumunu tutar;
etiket, maske, split veya manifest değiştirilmedi. Kaynak CARE durum
kodları ve maske/split politikası doğrulanmaktadır. Görevleri başarılı
skorlara göre seçme, maskeleri keyfi açma veya uygun olmayan görevleri
sessizce atlama yapılmaz. Kaynak anlamı düzeltmesi gerekiyorsa yeni
split/profile/suite kimliğiyle ayrıca kayıt ve gerçek Scorer kabulü gerekir.

## Paralel runtime işleri

Qwen araştırma profili ve çok satırlı kaynak üretimi ayrı Luna/high kolunda
hazırlanıyor. AOS gerçek driver incelemesi ayrıca canonical run-bound
Director unit adı ile GPU principal resolver'ın izin verdiği adlar arasında
uyumsuzluk buldu. Resolver düzeltmesi ayrı kaynak dilimidir; kimlik ve
sahiplik kontrollerini gevşetmeden gerçek CLI yolunu açmalıdır. AOS
değişiklikleri hâlâ yalnız izole kopyadadır. Holdout işi CARE semantik
incelemesiyle birlikte sürer; hiçbir açık M0 maddesi bu notla kapanmaz.

## CARE v3 kaynak düzeltmesi ve yeni kayıt

Resmî [CARE v6 açıklaması](https://zenodo.org/records/15846963), Farm A
status_type_id bilgisinin arıza günlüğünden türediğini ve eğitim filtresi
amacıyla kullanıldığını açıklar. Bu nedenle Farm A değerlendirme maskesi
artık yalnız eksik kaynak zamanları ve eksik seçili sensör ölçümlerini
kullanır. Eğitimdeki 0/2 durum filtresi korunur. Bu değişiklik Farm B/C'ye
uygulanmaz. Session, feature/split policy ve suite kimliği v3'e yükseltildi;
eski kayıtların anlamı değiştirilmedi.

Ayrı Luna kaynak kontrolü 22/22 CARE görevini materyalize etti. Tüm 12 PDM
görevinde maskelenmemiş pozitif örnek var. İlk receipt'teki
`healthy_unmasked` alanı toplam maskelenmemiş satır sayısıdır; sağlıklı
negatif maruziyet olarak yorumlanamaz. Düzeltilmiş ayrı v2 receipt ve ham çıktı, tüm PDM görevlerinde sağlıklı negatif maruziyetin de pozitif olduğunu doğrular (en az 143 satır); tüm NRM görevlerinde de sağlıklı maruziyet vardır. Bu yalnız kaynak kontrolüdür.
Kaynak yaması ve ilk kanıt SHA'ları `care-farm-a-status-025-main-source.json`
içindedir.

Main üzerinden gerçek 27 görevlik v3 kayıt/Planner/API dosya kontrolü
**session 58545 / exit 0**, **16,792 saniye** ile geçti. Manifest
**72.330.055 bayt**, matrisler **52.126.816 bayt**, RSS
**967.204.864 bayt**; hash
`a183c276742b35593866d99d743f83dec37f0f59079a70fa72e3627c48e0053c`.
Önceki v2 kayıtları korunur. Bu sonuç puanlama veya araştırma kabulü değildir.
Kanıtlar `default-public-suite-v3-025-check.json` ve
`default-public-suite-v3-025-command.json`.

## Run-bound principal düzeltmesi

Lab resolver canonical `swapp-ai-scientist-director-dispatch-<32hex>.service`
adını yalnız Lab sahibi için kabul eder. `resolve` ve `verify` çağrıları
sahipli isim doğrulamasını kullanır; PID/start/boot/InvocationID/cgroup
kontrolleri sürer. Gerçek ayrı systemd biriminde resolve+verify başarılı;
17 hedefli test/Ruff/py_compile exit 0. GPU, DB veya araştırma çalışmadı.

İki kaynak delta ayrı hash'lerle birleştirildi. İlk inceleme JSON'u ajan
hazırlığı sırasında yanlışlıkla üzerine yazıldığı için eski SHA'sı artık
byte olarak doğrulanamaz; bu dosya açık provenance notudur. İlk patch'in
özgün baytları geri getirildi ve root kaynak entegrasyon kaydı korunur.
Yeni `lab-director-principal-verify-fix-review.json` ayrı ve sabittir;
kaynak/log/probe eşliği root tarafından doğrulandı.

## Qwen çok satırlı kaynak ve araştırma profili

Sabit xgrammar 0.2.7, candidate_source metninde minLength veya maxLength
olduğunda ilk JSON satır sonu token'ını reddetti; bu sınırlar decoder
şemasından çıkarıldığında 15 satır sonu içeren aynı kaynak kabul edildi.
Trusted parser 1–6000 kaynak ve 1–384 hipotez karakter sınırlarını korur;
6001/385 negatif kontrolleri geçti. Prompt v5/schema v3 ve config hash'leri
bu değişikliği bağlar; tarihsel receipt'ler okunur, eski config ile yeni
model çağrısı başlatılmaz. Kanıt: `qwen-multiline-grammar-diagnostics.v1.json`.

Ek araştırma profilleri immutable registry config digest'iyle seçilir:
S1 8192 giriş + 2048 çıktı / model_max_len 10240; S2 8192 + 8192 / 16384.
Smoke profili ayrı kalır; toplam bölüm bütçeleri ve tek onarım korunur.
Bu kapasite gerçek GPU'da henüz kabul edilmiş değildir.

0.25.0 imaj byte eşliği 87 runtime dosyasında doğrulandı. Yedi kalite
komutu **session 5452 / exit 0**: **314 passed, 7 skipped, 13 deselected**,
strict mypy 78, Pylint 9.35. İmaj ve kaynak bağı
`parallel-integration-025-quality-gate-binding.json` içindedir.
İmaj launcher'ındaki source_unchanged=false yalnız imaj pin dosyasının
başarılı build sonrası güncellenmesidir; build'in kendi 87 dosya eşliği
ve son kalite kapısının kaynak sabitliği true'dur.
Gerçek Qwen bölümü ve tam v3 Scorer kabulü ayrı çalıştırılır.

## Gerçek 0.25 model denemesi — canlı AOS çakışması

**Session 54140 / exit 1**, **100,747 saniye**. 90,688 saniyede canlı AOS
model dizinindeki `llama-server` (PID 3196279, `session-28.scope`) ortak
kuyruğa kayıtlı olmadan 210 MiB VRAM kullanımıyla görüldü. Gözlemci yalnız
kendi Lab parent/model birimlerini durdurdu; yabancı AOS sürecine dokunulmadı.
Son GPU 62 MiB, kaynak/runtime hash'leri aynı, sahipli model cgroup'ları
boşaldı. Geçerli/başarısız bir öneri yanıtı oluşmadı; bu kesinti decoder
veya S2 başarısı/başarısızlığı olarak yorumlanmaz. Private test ticket'ı
active kaydıyla saklandı; yapay done yazılmadı.

Kanıt: `local-qwen-provider-025-episode-review.json` ve gerçek CLI exit
kodunu bağlayan `local-qwen-provider-025-outcome.json`. İlk dosya 0.25
commit'ine dahil edilmiştir. Kullanıcıya canlı AOS broker koordinasyonu
veya ayrı GPU test penceresi soruldu. Canlı AOS değiştirilmiyor; CPU
Scorer/holdout/eğitim kodu işleri devam ediyor.

## Gerçek v3 public sandbox/Scorer sonucu

**Session 35503 / exit 0**, **394,561 saniye**: **27/27 görev** tamamlandı
(12 PDM, 10 NRM, 5 EVT). Robust-z seed 0, her görev için taze Docker
fit/score, determinism/causality/hardcoding kontrolleri ve ayrı systemd
Scorer worker kullanıldı; **27/27 guard grubu** geçti. CARE 010 dahil tüm
v3 görevler gerçek skor verdi. Private koşu sonunda yalnız kendi run
satırı temizlendi; dataset kayıtları korundu, kaynak hash'i değişmedi.

Ayrı `public-execution-025` kopyası commit `221e5d9` ve aynı kaynak/imaj
hash'ini kullandı. Parent 2 GiB RAM/1 CPU/swap 0 ve toplam 1950 saniye
systemd sınırı altındaydı; Scorer ve Docker ayrıca kendi sınırlarını
kullandı. Bu sonuç, üç baseline × üç seed kalibrasyonu veya tam LLM /
Referee / holdout / AOS araştırma kabulü değildir. Kanıt:
`public-sandbox-scorer-025-outcome.json`, `public-sandbox-scorer-025-review.json`
ve `public-sandbox-scorer-025-command.json`.
