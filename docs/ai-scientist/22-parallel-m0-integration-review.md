# Paralel M0 entegrasyon incelemesi

Tarih: 2026-09-25. Başlangıç kaynağı `c0a9565`; ürün kodu 0.19.0 / `92e2bdc` ile aynıydı. Bu kayıt devam eden dalların incelemesidir; aşağıdaki maddeler tek başına kabul veya başarılı entegrasyon kanıtı değildir.

## İş bölümü ve sınırlar

Kullanıcının hızlandırma talebiyle üç **GPT-6 Luna / high** ajanı ayrı Git worktree'lerinde çalışıyor:

| Kol | Ürün kapsamı | Ayrı PostgreSQL veritabanı |
| --- | --- | --- |
| `feat/luna-m0-recovery` | Director sahiplik kaydı/kurtarma; önce küçük S2 örnekleme düzeltmesi | `swapp_lab_m0_recovery_b1ef2b5e` |
| `feat/luna-m0-report-replay` | HTML rapor, karar girdilerinin kalıcı kaydı ve tam replay | `swapp_lab_m0_report_replay_62677102` |
| `feat/luna-m0-public-suite` | Gerçek veri yükleyicileri, PDM/NRM Scorer ve süit ağırlıkları | `swapp_lab_m0_public_suite_3ddaa2d8` |

Root inceleme, kaynak birleştirme ve gerçek donanım doğrulamasını yürütür. Paylaşılan dondurulmuş Python ortamı değiştirilmez. Ağır CPU kontrolleri ortak `cpu-check.lock` ve sınırlı systemd birimleriyle sıraya alınır. GPU denemelerini root koordine eder. Canlı AOS kaynağı ve tarihsel Lab koşuları bu iş bölümü sırasında değiştirilmez. Veritabanı izolasyon kaydı: [parallel-m0-isolated-databases.json](review-evidence/parallel-m0-isolated-databases.json).

## Entegrasyon öncesi bulunan açıklar

| Alan | Somut bulgu | Birleştirme için gereken doğrulama |
| --- | --- | --- |
| Kurtarma | JSON receipt içindeki `run_id` metni doğrudan UUID ile karşılaştırılıyordu. Geçerli terminal belgeler yanlışlıkla bozuk sayılabilir. | Gerçek PG'de geçerli belge çifti, ölü sahip ve ayrı Scorer finalizer ile pozitif yol; eksik sahipte değişmeyen run/pending yolu. |
| Sahiplik | MainPID'nin yokluğu tek başına alt süreçlerin bittiğini göstermiyor. Aynı unit'in yeni nesli ayrıca korunmalı. | Persist edilmiş boot/PID-start/InvocationID/cgroup; recursive `cgroup.events populated=0`; canlı veya yeni nesilde pending. |
| Replay | İlk taslak seed kayıtlarını yalnız `(seed,task)` ile ayırıyordu. Baseline, primary ve confirmation kimlikleri karışabiliyor. | `(evaluation_kind,seed,task)` anahtarı; gerçek Scorer satırları, çıktı blobları ve kalibrasyonla hash ve sayı eşliği. |
| Replay | Primary ekranı parent seed 0 kullanır; teyit parent seed 0 ve 1 ortalamasını kullanır; noise seed 0–2'den gelir. Her aşamada aynı parent seed kümesi kullanılamaz. | Seed 0 ve 1 skorları farklı parent ile gerçek production writer→reader testi; primary/confirmed ayrı provenance. |
| Rapor | İlk SVG diyagonal çizgiler ve sıra konumları kullanıyordu. | Deney sequence değerlerinde H/V merdiven; verdict sayıları ve nihai ölçülmüş skorlar ledger ile eşleşmeli. |
| CARE boşlukları | Gözlenmiş satırdaki eksik sensör değeri sonlu dolgu sonrası maskesiz puanlanabiliyordu. `ffill(limit=0)` desteklenen sıfır ayarıyla da çelişiyordu. | Eksik sensör ve uzun boşluk maskeleri dolgu öncesinde korunmalı; gerçek zaman ızgarası ve train segment politikası kaydedilmeli. |
| Migration | PostgreSQL `CREATE OR REPLACE VIEW` eklenmiş view sütunlarını downgrade sırasında kaldıramaz. | İzole DB'de upgrade→downgrade→upgrade; gerekli view grant ve bağımlılıklarının korunması. |
| Süit ağırlığı | EVT/PDM/NRM metrik tipi bağımsız kaynak ailesi değildir; dosya sayısı yapay aile yaratamaz. | Tip payları .5/.3/.2, tier katsayıları 1/.6/.3 ve sonra .25 aile tavanı; son tip/aile payları manifestte. Dörtten az ailede ret. |
| Etiket tier | Public task için varsayılan `gold`, kaynak kanıtı olmadan uzman onayı iddiası yapabilir. | Her kaynak için açık ve gerekçeli tier; simüle CATSv2 `bronze`. |
| NRM guard | Eski EVT runner'ı `position_bias=pass` sabitliyordu. NRM eklenince bu yol artık geçerli değil. | Tüm beklenen NRM çıktılarını kapsayan guard; en az yarısında zaman rampası bulunan süitte REJECT; eksik kümede pass yok. |
| NRM eşik | Yeni helper taslağı 0.5 kullanıyordu; spec guard tablosu `>0.8` ister. | 0.8 açık replay config/hash alanı; 0.5–0.8 aralığını yanlış reddetmeyen test. |
| Replay başlangıcı | Okuyucu üç baseline seed skorunun maksimumunu alıyordu; loop ortalamasını kullanır. | Aynı toplama/bölme sırası; birbirinden farklı üç seed ile regresyon kontrolü. |
| Güvenilir kaynak kapsamı | Yeni üst düzey report/replay modülleri mevcut fingerprint ve açık mypy dosya listesinde yoktu. | Yeni modüller hash ve strict kalite kapsamına eklenmeli; `lab report` / `lab replay` CLI yolları doğrulanmalı. |

## S2 için dar kritik yol

[Önceki gerçek tanı](21-local-qwen-s2-research-review.md), kurulu vLLM split top-p yolunda zorlanan düşünme sonu token'ının kaybolduğunu gösterdi. Sabit `top_k=20` tanıda marker'ı doğru üretti; 2048 toplam token nihai JSON için yine yetersizdi. Entegrasyon adayı bu yüzden sabit ve doğrulanan `top_k=20`, 512 düşünme bütçesi ve 4096 toplam çıktı token'ıdır. Model bağlam tavanı ve süreler aynı profile, istek/config hash'lerine ve receipt'lere bağlanmalıdır.

Bu profil henüz başarılı ürün S2 kabulü değildir. Kaynak donduktan sonra tek gerçek S2 çağrısının JSON şeması, token/süre sınırları ve GPU drain'i ölçülür. Ardından en az altı gerçek önerili araştırma ve AOS ile dönüşümlü gerçek çalışma kabulü sürer. Önceki 512/2048-token başarısız kanıtlar korunur.

### İlk 0.20 aday profil denemesi

Luna'nın beş dosyalık S2 dilimi hash eşliğiyle ana çalışma alanına alındı; harness sürümü 0.20.0 adayıdır. İzole kolda native/provider hedefli testleri 53 geçti; Ruff ve mypy başarılı bildirildi. Bu hedefli sonuçlar tam kalite kapısı değildir.

Komut: `.venv/bin/python docs/ai-scientist/review-evidence/review_local_qwen_s2_candidate.py --output docs/ai-scientist/review-evidence/local-qwen-s2-profile-v2-review.json`.

Gerçek session **63534 / exit 1**, **32.356 saniye**. Tokenizer 442 giriş token'ını doğruladı; model başlatma katmanı kayıtlı 6144 sınırını eski `{4096,24576}` izin listesi nedeniyle reddetti. Hata `unit_launch / ValueError: model maximum length must come from a registered profile`. Model süreci ve inference oluşmadı. Kaynak ve kurulu runtime hash'leri sabit, supervisor terminal, dış GPU tüketicisi yok.

Özel test veritabanındaki ticket `active` kaldı; belirsiz başlatma kaydını elle tamamlanmışa çevirmedik. İzin listesinin kayıtlı profillerden türetilmesi ve bilinen başlatma öncesi hatanın admission öncesinde doğrulanması uygulama ajanına iletildi. Kanıt: [local-qwen-s2-profile-v2-review.json](review-evidence/local-qwen-s2-profile-v2-review.json). Bu sonuç S2 kapasitesi veya geçerli model çıktısı kabulü değildir.

### İkinci gerçek deneme ve dondurulmuş 0.20 dilimi

Luna başlatıcının izin listesini `MODEL_TURN_PROFILES` üzerinden türetti; 4096 ve 6144 değerlerinin gerçek `--max-model-len` argümanına geçiş testi eklendi. Aynı driver yeni `local-qwen-s2-profile-v2-after-review.json` hedefine çalıştırıldı: **session 19049 / exit 1**, **196.756 saniye**, **19/23 kontrol** geçti.

Bu kez gerçek model çalıştı: 442 prompt ve 4096 completion token, düşünme içeriği yalnız 1981 karakter uzunluk bilgisiyle kaydedildi. Nihai içerik 16893 karakter oldu ve `finish_reason=length` nedeniyle reddedildi. Ham düşünme/nihai yanıt bu review kanıtına yazılmadı. Tek model nesli doğrulandı, ticket `done`, cgroup boş ve GPU sonunda 62 MiB. Örneklenen toplam GPU zirvesi 12832 MiB, model MemoryPeak 10 GiB, en düşük kullanılabilir host RAM 19,648,610,304 bayt; bütün rezerv kontrolleri geçti. Bu tek model denemesi AOS ile gerçek birlikte çalışma değildir.

İstem hâlâ 65536 karakterlik `candidate_source` ve 2000 karakterlik hipoteze izin veren genel şemayı kullanıyor. Çıktı bütçesiyle uyumlu, tam çalıştırılabilir ama kısa aday istemi/yerel şema ayrı sonraki düzeltmedir; JSON kesilerek başarılı sayılmaz ve süre/token sınırları körlemesine yükseltilmez.

0.20 kaynaklarının sandbox imajı **`sha256:cafb4f9c5b24f7598ec3ae7ebafa49b057f7a2d5dc9e3be74e41ff33569f8aa2`**, build/parity **session 6186 / exit 0**. Tam kalite kapısı **session 1857 / exit 0**: 239 test, 11 deselected, mypy 69 dosya, Pylint 9.32; yedi komutun tamamı exit 0. Harness hash **`785c50737d514718a6127646cedbf6d8807f9f9044c84259a73c9eb3ea4d7201`**. [Kaynak ve kanıt bağı](review-evidence/local-qwen-s2-020-quality-gate-binding.json) S2 başarısızlığını açık tutar. Paralel public/report/recovery kodları henüz bu sürüme alınmadı.

### 0.20 ile gerçek birlikte tanı

S2 tanı profili düzeltmesi gerçek ortak broker üzerinde de doğrulandı:
**session 52269 / exit 0, 12/12 kontrol, 211,187 sn**. Dört çağrı
Qwen S1 → Decider → Qwen S2 → Bonsai sırasında başarılı. S2 geçerli
aritmetik JSON'u 140 token'da verdi; bu araştırma CandidateProposal değildir.
GPU zirve 12754 MiB, son 62 MiB; RAM/disk/sıcaklık rezervleri korundu.
Kaynak fingerprint'i önceki başarılı 0.20 kalite kapısıyla aynıdır.
`20-aos-broker-model-review.md` kapsamı ve özel durum arşivini kaydeder.

## Birleştirme sırası

### Dondurulmuş kompakt yerel istem — 0.21

Luna'nın üç dosyalık dilimi hash eşliğiyle alındı: `local_llm.py`,
`fake_llm.py`, `test_local_qwen_provider.py`. Genel CandidateProposal
sözleşmesi aynı; yalnız yerel model şeması hipotezi 384, tam kaynak kodunu
6000 karakterle sınırlar. İstem v3, şema/config hash'i ve primary/repair
parser'ları aynı sınırları kullanır. Kaynak kesilmez; tarihsel v2 receipt'ler
okunabilir. Probe'a ayrıca Python AST ve `build_candidate` varlık kontrolü
eklendi; bunlar aday kodunun çalıştırılması değildir.

İmaj build/77 runtime dosyasında byte eşliği **session 76562 / exit 0**:
`sha256:6f927103a7b33b9f3f6bc9de1a682575ecc8481e03ca28cdddf29956389bd09d`.
Tam kalite kapısı **session 50789 / exit 0**, **242 test**, 11 deselected,
strict mypy 69 kaynak, Pylint 9.32; yedi komutun tamamı geçti.
Harness hash `5c08ed4c66a88c0c407ebba8acf0b32c74c5bdc80669e822a69752e90fa82464`.

Tek gerçek S2 probe **session 20177 / exit 1**, **125,489 sn**, **11/24 kontrol**.
114,830 saniyedeki gözlemde test dışındaki `/home/cachyos/.venv/bin/python`
PID 2904729, `session-26.scope` içinde 1180 MiB GPU belleği kullanıyordu.
Gözlemci yalnız kendi parent/model birimlerini durdurdu. Dış süreç
değiştirilmedi; kaynağın AOS olduğu doğrulanmadı. Son GPU 62 MiB ve test
cgroup'ları boş. Nihai peer yanıtı/öneri yok; özel tanı ticket'ı `active`
kaldı, elle tamamlanmışa çevrilmedi. Bu kesinti kompakt istemin başarı
veya model kapasitesi kanıtı değildir. `local-qwen-compact-021-s2-review.json`
ve `local-qwen-compact-021-quality-gate-binding.json` kaynak/sonuç bağını tutar.
Diğer paralel ürün dalları hâlâ bu sürüm dışında.

### Kompakt S2 tekrar ölçümü

Aynı 0.21 ürün kaynaklarıyla tek çağrı tekrarlandı: **session 27836 / exit 1**,
**176,745 sn**, 512 prompt / 2837 completion token, `finish_reason=stop`.
Bu kez dış GPU tüketicisi yoktu. Yanıt JSON sözleşmesi ve seçili hareket
kontrolünü geçti; üretilen aday Python kaynağı `ast.parse` aşamasında reddedildi.
Kaynak çalıştırılmadı, Scorer ölçümü yapılmadı. GPU zirvesi **12854 MiB**,
en düşük kullanılabilir RAM **19.313.631.232 bayt**, en yüksek sıcaklık **63°C**;
ticket `done`, sahip olunan model süreçleri boş ve son GPU **62 MiB**.
Kanıt: [local-qwen-compact-021-s2-retry-review.json](review-evidence/local-qwen-compact-021-s2-retry-review.json).

Eski probe başarı alanlarını AST kontrolünden sonra yazdığı için bu kayıtta
`successful_candidate_schema_parse=false` görünür. Hata konumu JSON parse'ın
başarılı olduğunu gösterir; ham kayıt değiştirilmedi. Sonraki probe sürümü
JSON ve Python doğrulamasını ayrı kaydeder; kaynak uzunluğu/satır sayısı ve
yalnız sözdizimi hata konumu tutar. Ham kaynak veya düşünme kaydedilmez.
Bu başarısızlık kaynak sınırının kesme etkisi ya da başka bir sözdizimi hatası
olabilir; mevcut kayıt kesin nedeni ayırmaz. S2 araştırma kabulü açık kalır.

### Veritabanı zinciri ve sonraki birleştirme

Salt okunur ek kontrol, ana `swapp_lab` veritabanının geçici
`0018_public_task_semantics` revision'ında olduğunu, fakat `0016` Director
sahiplik tablosunun bulunmadığını gösterdi. Semantics tablosu boş, sekiz
profilin tamamı EVT. Ana kaynakta henüz 0016/0018 migration dosyaları yok.
`main-db-migration-020-readonly-review.json` bu uyuşmazlığı kaydeder; hiçbir
şema veya koşu değiştirilmedi. Dalların testleri ayrı DB'lerde sürer.
Özel 0600 `pg_dump` yedeği alındı ve yeni
`swapp_lab_m0_migration_rehearsal_ab103ab3` DB'sine gerçek `pg_restore`
**session 54582 / exit 0** ile açıldı. Koşu, deney, rapor, etiket, skor,
profil ve semantics satır sayıları ile revision eşleşti. Kanıt:
`pre-integrated-migrations-backup.json`. Bu geri yükleme yedeği doğrular.

Ardından yalnız bu clone'da geçici 0018→0015 downgrade ve gerçek
0015→0016→0018 upgrade **exit 0, 6/6 kontrol** ile tamamlandı.
Kullanılan 0016 hash'i `764ae650…`, 0018 hash'i `b22838af…`.
Eski tabloların tüm canonical satır içerik hash'leri ve sayıları eş;
iki yeni kurtarma tablosu boş, Director'ın dev-view SELECT yetkisi korunuyor.
512 MiB/1 CPU/swap 0/120 sn sınırındaki komut
`review_parallel_migration_chain.py`; kayıt
`parallel-migration-chain-rehearsal.json`. Ana DB değişmedi.

0016 incelemesinde iki nullable CHECK'in SQL UNKNOWN nedeniyle kısmi
sahiplik veya JSON-without-digest kabul edebileceği bulundu. Luna açık
NOT NULL şartları ekledi; düzeltilmiş private PG üzerinde kısmi NULL
insertlerinin reddi dahil 16 hedefli test başarılı bildirildi. Bu branch
testleri henüz ana kaynak kalite kapısının parçası değildir.
Ürün birleştirilirken önce mevcut DB'nin yedeği ve eski geçici zincir
doğrulanmalı; son `0015 → 0016 → 0018` zincirine veri kaybetmeden geçilmesi
gerekiyor. Revision yalnız stamp edilerek başarı sayılmaz.

1. Kısa S2 ürün dilimini hedefli test ve kaynak hash listesiyle dondur; gerçek GPU çağrısını diğer CPU geliştirmesiyle paralel yürüt.
2. Diğer dalların sahip olunan dosyalarını ve gerçek test exit code'larını al. CLI hunks'larını birleştir; migration zinciri `0015 → 0016 → 0018` olur. `0017` kullanılmaz.
3. Yeni harness sürümü, kaynakla eşleşen sandbox imajı ve yedi komutun tümünde gerçek exit 0 kalite kapısı oluştur. Önceki 0.19 gate yeni dalları kapsamaz.
4. Kabul tablosunu yalnız komut/çıktı/artefakt ile kanıtlanan sonuçlara göre güncelle. Gerçek public suite, tam araştırma, holdout, eğitim, AOS birlikte kullanım ve PR gereksinimleri ayrı açık kalır.


## Birleştirilen 0.22 dilimi

Recovery, public-suite ve report/replay kaynakları hash kontrollü olarak ana
feature branch'e alındı; iki CLI değişikliği birlikte korundu. İlk listeler
`recovery-integration-source-022.json`, `public-suite-staged-freeze.json` ve
`report-replay-integration-source-022.json` içindedir. Aşağıdaki düzeltmeler
bu dondurulmuş listelerden sonradır.

### Kurtarma ve ana DB

Dispatch, run kimliğine bağlı sınırlı systemd biriminde çalışır; PID başlangıcı,
boot, InvocationID ve cgroup sahipliği claim ile saklanır. Yerel Qwen GPU
principal'ı aynı birime bağlı değilse claim yapılmaz. İzole PG/systemd
kontrolleri canlı/yeni nesil sahipleri, alt süreç drain'ini, eksik sahipliği,
idempotent finalizer'ı ve nullable CHECK'leri kapsar. Son üç dispatch kontrolü
exit 0; `recovery-dispatch-final-focus.json` fixture sınırlarını açıklar.

Yeni model varsayılanları eski canonical JSON'u yeniden serileştirirken
değiştiriyordu. İlk gerçek CLI inspect altı terminal çifti yanlışlıkla bozuk
saydı (`integrated-recovery-legacy-inspection.json`). Düzeltme özgün JSON'un
canonical biçimini, duplicate key ve sonlu sayı şartlarını model parse'ından
önce denetler; hash/kimlik kontrolleri korunur. Tekrarlanan iki gerçek CLI
inspect **session 57980 / exit 0, 8/8**: altı çift geçerli, owner kanıtı eksik,
finalizasyon engelli; tarihsel durumlar ve eksik deney aynen korunuyor.
`integrated-recovery-legacy-inspection-after.json`; DB/blob değişikliği yok.

Özel yedek/restore provasıyla ana DB başlangıç durumu eşleştirildi. 0018 biçim
değişikliğinin SQL sabitleri dahil AST eşliği doğrulandı. Geçici 0018→0015
downgrade ve son 0015→0016→0018 zinciri **session 32834 / exit 0, 5/5**:
tüm eski tablo satır hash/sayıları korundu, yeni owner tabloları boş,
dev-view grant'i mevcut. `alembic check` exit 0. Kanıtlar
`parallel-migration-chain-main-integration.json` ve
`parallel-integration-022-alembic-check.json`. İlk kaynak hash uyuşmazlığı
DDL'den önce reddedildi; stamp veya elle owner doldurma yapılmadı.

### Public materyalizasyon ve aile metrikleri

Gerçek CARE Farm A: **22 görev (12 PDM, 10 NRM)**, altı adlandırılmış fiziksel
ortalama özelliği, 50.880 eval/21.525 maskeli/341.606 sağlıklı kesintisiz train
örneği, peak RSS 444.387.328 bayt; exit 0. Farm B/C açılmadı, arşiv ikinci
ağaca çıkarılmadı. Gerçek SMD: etiketlere bakmadan seçilen ilk makine ve
12.000 train/12.000 test penceresi, doğrulanmış 60 saniye aralığı, peak RSS
289.112.064 bayt; exit 0. `care-public-materializer-check.json` ve
`smd-public-materializer-check.json`.

Ayrı PG'de iki PDM/NRM Scorer testi exit 0; etiketler bu testlerde sentetiktir.
Manifest .5/.3/.2 tip paylarını, 1/.6/.3 tier katsayılarını ve .25 bağımsız
aile tavanını kaydeder. NRM guard tüm beklenen görevleri ve .8 eşiğini kullanır.
Promoted parent ve checkpoint okuyucularındaki EVT/VUS varsayımları giderildi;
PDM/NRM task_score ve aile metrikleri kullanılır, sonlu olmayan süreler
reddedilir. On yeni checkpoint testi geçer. Tam SKAB + dört TSB kaynağı + CARE
araştırma kabulü açıktır; profil ticari olmayan, GHL/SWaT hariçtir.

### Gerçek report/replay yolu

İzole worktree'de gerçek PG, guarded Docker ve ayrı Scorer ile **9 baseline +
6 proposal ölçümü** tamamlandı. Baseline parent'tan KEEP, ardından farklı
promoted parent'tan KEEP_SIMPLER çıktı. Seed 0/1 ham VUS skorları
0.5924702112196091 ve 0.7435861865748059; primary yalnız seed 0, confirmation
ayrı seed 0/1 kanıtlarını ve normalize ortalamayı kullanır. Dört karar bit
eşliğiyle replay edildi; eksik/değiştirilmiş manifest reddedildi.

Gerçek `python -m lab.cli report <run_id>` ve `replay <run_id>` fixture
temizlenmeden exit 0 verdi. HTML/API/CLI SHA eş:
`79234737eccf752396ca101adc36d5798f3f978efd30d33225e7e16fd3aea0c5`.
Gerçek sequence değerlerinde H/V merdiven ve sayılar (3 baseline, 2 öneri,
1 KEEP, 1 KEEP_SIMPLER) eşleşti. Bounded koşu **exit 0, 20/20**, 3:01.472,
peak 320,3 MiB. Kanıt `report-replay-production-pg-cli-022.json`, HTML aynı
adla; değişmemiş driver `.py.txt` olarak arşivlendi. Driver'ın genel command
alanında eski unit adı kalmıştır; final launch kaydı ayrıca korunur.
Bu izole EVT fixture kanıtıdır; kendi image/hash kimliği kayıtta korunur.
Main public/gerçek model kabulü değildir. Önceki driver hataları (yanlış
seed-mean assertion dahil) private failure-history altında saklıdır.

### S2 tanısı ve kalite geçmişi

JSON/Python doğrulamasını ayrı kaydeden probe **session 44215 / exit 1,
41,368 sn**. 39,545 saniyede ortak kuyruğa katılmayan Python PID 3024270,
`session-26.scope` içinde GPU kullandı. Model yanıtı yok; owned süreçler
boşaldı, dış sürece dokunulmadı. Son örnekte dış tüketici 3968 MiB/toplam
4038 MiB; sonraki kontrolde süreç bitmişti. AOS kaynaklı olduğu doğrulanmadı.
`local-qwen-022-s2-syntax-diagnostic.json` S2 veya GPU temizlik kabulü değildir.

İlk birleşik gate 14 fingerprint fixture hatası ve wrapper PATH'inde eksik
uv ile başarısız oldu. Fixture/PATH düzeltmesinden sonra **session 57356 /
exit 0**, 276 test/77 mypy dosyası/yedi komut geçti; bu kaynaklar son
family/recovery düzeltmelerinden öncedir. Ayrı `before-family-recovery`
kalite kaydı korunur. Sonraki **session 61647 / exit 1** eski resume fixture'ında
eksik family ve tuple tür çıkarımını buldu. **Session 77438 / exit 1** içinde
290 test geçti, iki tuple-index mypy hatası kaldı. Değişken uzunluklu metrik
listesiyle dar tür düzeltmesi yapıldı; hedefli mypy exit 0. Her iki başarısız
tam gate, adında `final`/`verified` olsa da başarısız olarak korunur.


### Son başarılı kaynak sınırı

**Session 34679 / gerçek exit 0**: kaynakla eşleşen imaj build/parity ve
son tam gate tamamlandı. **290 test, 7 skip, 13 deselected**, strict mypy
**77 dosya**, Pylint **9.35**, yedi komutun tamamı exit 0. Gate boyunca
133 kayıtlı kaynak değişmedi. İmaj wrapper'ındaki tek değişen dosya beklenen
`ops/sandbox-image.lock`; imajın kendi 85 runtime dosyasında byte eşliği
ve build sırasında kaynak sabitliği ayrıca doğrulandı.

İmaj `sha256:f40f9df58ed8d0bd2319286ac1fbda8f0dfcbdfe198670aca5e14caf635e6f96`;
harness `fb1841b7405206f96b8e8b5dce54b4a62df5badaf8aacd8ef8666284d4ea4bd5`.
`parallel-integration-022-quality-gate-binding.json` final imaj/kapıyı,
kaynak listelerini ve kapsamları ayrı kanıtları bağlar. Report/replay gerçek
launch/CLI exit kodları `report-replay-pg-cli-launch-wrapper.json` içindedir;
stdout JSON nesneleri özgün driver'dan, yeniden biçimlenen stdout satırları
ise açıkça yeniden üretim olarak etiketlenmiştir.

M0 tablosu **3 geçti / 15 kısmi / 4 açık**: report/replay kısmiye ilerledi.
Holdout, tam public suite, başarılı araştırma S2/altı öneri, AOS araştırma +
desktop birlikte kabulü, eğitim/kapasite ve PR koşulları tamamlanmadı.
