# Teslim odaklı durum incelemesi ve yeniden planlama

2026-10-01. Kullanıcı geliştirmeyi durdurup neden ilerlenmediğinin
incelenmesini istedi. Goal `paused`; yeni deney, kod değişikliği, deploy,
push veya merge başlatılmadı. Bu inceleme uygulama kabulü değildir.

## Doğrulanan durum

- Scientist HEAD `f23e1f6fc723c6d3bc5880eaea34b429dece5d62`; inceleme
  başlangıcında worktree temiz. AOS salt okunur HEAD
  `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
- M0 kayıt özeti **11 geçti / 7 kısmi / 4 açık**. 11/22, yalnız bu
  kabul listesinin oranıdır; bütün ürünün %50 tamamlandığını göstermez.
  OM ve açık laboratuvar gereksinimleri ayrıca geçerlidir.
- Son kalite kaydı: 2121 passed, 7 skipped, 120 GPU-live deselected;
  yedi komut exit0. GPU kabulü bu kapıda çalıştırılmadı.
- CPU ürün çıktıları mevcut: sentetik LSH/OPTICS/SOM→NN→OMR raporu,
  dört gerçek veri kaynağında baseline raporu, CPU kesinti/devam örneği,
  CLI/API/arayüz. Bunlar yerel model araştırması değildir. Ayrıntılar
  `43-first-project-guide.md` içinde; bu incelemede yeniden koşulmadı.
- Yerel Qwen ile tam S1/S2 araştırması, gerçek AOS etkileşimli göreviyle
  adil birlikte çalışma, araştırma holdout'u ve eğitim kabulü açık.
- Son araştırma `6fb469be-1c7d-48b7-a9b9-dbef0df8adfc`: Director,
  parent ve geçici API unit'leri inactive/MainPID0. Parent tool handle34913
  bulunamadı. Beklenen `director-result.private.json` yok; private log boş;
  geçici PG Docker listesinde yok ve bağlantı OperationalError verdi.
  Terminal ledger, kapanış nedeni ve cleanup receipt doğrulanamadı.
  Bu gözlemler başarı veya güvenli GPU release kanıtı değildir. Yeniden
  başlatılmadı; diğer oturumların süreçlerine müdahale edilmedi.
- İlerleme JSON'u 03:20 UTC'deki eski gate/GPU engeli durumunu taşıyordu;
  sonraki teslimi ve denemeyi yansıtmıyordu. Bu incelemeyle güncellendi.

## Neden teslim yavaşladı?

1. **Uçtan uca teslim yerine bileşen kapanışı optimize edildi.**
   Son Scorer izolasyonu gerçek bir düzeltmedir; yine de kabul toplamı
   değişmedi. Gereken güvenlik düzeltmeleri, kullanıcıya görünen bir
   tamamlanmış araştırmaya bağlanmadan peş peşe üretildi.
2. **Çalışma alanı fazla genişledi.** 147 lab Python dosyası,155 test
   dosyası ve1224 review-evidence dosyası var. Sayılar tek başına israf
   kanıtı değildir; fakat ürünün kritik yolu kapanmadan belge/test/adapter
   yüzeyinin büyüdüğünü gösterir. Paralel görevler aynı teslim sonucuna
   bağlanmalı; yeni sözleşme ve sürüm alanları açmamalı.
3. **Gerçek çalışma maliyeti admission bütçesine yansıtılmadı.** Son
   canlı gözlemde bağımsız Scorer yaklaşık1s, ardışık işler arası toplam
   fit/guard/orkestrasyon yaklaşık33s idi.36 baseline ≈1188s;1200s bütün
   koşu bütçesi model ve adaylara çok az zaman bırakır. Bu bir tahmindir;
   kayıp ledger nedeniyle son denemenin kesin kapanış nedeni değildir.
   Önceki somut süreç hatası da önemlidir: `62-fresh039-public-campaign.md`
   terminal kaydında243/243 baseline tamamlanmış, fakat Director `hparam`
   isterken fake fixture `features` döndürdüğü için ilk öneriden önce run
   failed olmuştur. Bu ucuz hareket/sözleşme kontrolü uzun baseline'dan
   önce yapılmalıydı. Bu fixture hatası gerçek Qwen başarısızlığı değildir.
4. **Bağımsız Scientist teslimi AOS ayrıntılarıyla fazla iç içe ele alındı.**
   AOS DesktopRuntime gerçek checkout'un `data/` altında ayrı workspace
   ister (`src/aos/desktop.py:50–53`). AOS salt okunur kuralı altında bu
   gerçek bir entegrasyon sınırıdır. Scientist-only araştırmayı engellemez.
   İzole yamalı kopya actual AOS kabulü yerine kullanılamaz.
5. **Durum ve sonuç sahipliği eksik kaldı.** İzole DB koşusu ana arayüzden
   otomatik izlenmiyor; ilerleme metni elle güncelleniyor. Son denemede
   parent sonucu yok. Kullanıcı hangi işin çalıştığını ve neden durduğunu
   tek ekrandan güvenilir biçimde göremiyor.
6. **Kapsamın teslim sırası net tutulmadı.** M0, OM/istatistik, genel model
   kataloğu, skill ve LoRA/QLoRA birikimi aynı anda açık tutuldu. Kullanıcı
   kapsamı korunmalıdır; fakat bir sonraki somut teslim seçilmelidir.

Bu sıralama ve çalışma düzeni orkestrasyon hatasıdır. Model adını değiştirmek
ve daha fazla agent eklemek tek başına bunları düzeltmez.

## Devam için plan — kapsamı koruyarak sıralı teslim

| Sıra | İş | Kullanıcıya görünen çıkış koşulu |
|---|---|---|
| 0 | Son koşunun kayıp sonucunu ve runtime/ledger durumunu uzlaştır; mevcut ana servisleri yeniden doğrula | Koşu için terminal kanıt veya açık unresolved kaydı; GPU cleanup kanıtsızsa release yok; UI gerçek durumu gösterir |
| 1 | Mevcut kaynak ve doğrulanmış model/backend ile bağımsız gerçek araştırmayı tamamla; ölçülmüş baseline maliyetinden yeni, sınırlı run bütçesi çıkar | Gerçek S1 ve S2 önerileri→Docker deneyleri→bağımsız Scorer→Referee→rapor/replay; kullanıcı arayüzden sonucu açabilir |
| 2 | Aynı çalışan yolun kontrollü iptal/devamını tamamla | stop_requested ile terminal ayrımı, owner/generation fencing, alt süreç drain ve GPU release/karantina kanıtı; sahipsiz eski run'lara dokunulmaz |
| 3 | AOS oturumuyla actual checkout ve tek sözleşme sürümünü sabitle; workspace desteğini o oturum sağlasın | AOS görev→GPU devri→Scientist araştırma→AOS rapor doğrulama→temiz kapanış; iki iş ilerler, gecikme/VRAM ölçülür |
| 4 | Çalışan araştırmayı seçilmiş gerçek public kaynaklar ve holdout'a taşı | Genesis/GECCO/CATSv2/SMD üzerinde araştırma ve ayrı holdout raporu; baseline tamamlanması araştırma sayılmaz |
| 5 | OM/istatistik/DB seçim akışını aynı agent yoluna bağla | Kullanıcı parametreli LSH/OPTICS/SOM, mod toleransı→NN→sürekli OMR ve anomali deneyini başlatır, uzun işi izler, durdurur, raporlar |
| 6 | Genel açık çekirdek teslimini ve gelişim zincirini tamamla | Taşınabilir kurulum, seçilmiş lisans/CI, kayıt uygunluğu, model değişimi, skill ve adapter değerlendirmesi kendi ayrı kanıtlarıyla kapanır |

Birinci gerçek araştırma çalışır teslimdir; tüm goal'un tamamlanması değildir.
M0.13'ün ≥6 öneri ve S1/S2 dağılımı dahil özgün kabul eşikleri korunur.
Eğitim smoke testi öğrenilmiş adapter olarak sunulmaz.

## Yeni çalışma düzeni

- Bir aktif uçtan uca teslim; diğer tüm görevler bu teslimin somut engelini
  kaldırır. Yeni genel framework/adapter/sözleşme geliştirmesi sıraya alınır.
- Orkestratör tek gerçek GPU yürütücüsü. Bir worker kritik runtime/yaşam
  döngüsü engelini, diğeri aynı koşunun UI/rapor görünürlüğünü sahiplenir.
  Aynı dosya/süreç üzerinde paralel müdahale yapılmaz.
- Önce mevcut CLI/servis yolunu kullan; yeni özel test sürücüsü ancak
  mevcut yolda gerçekten eksik olan gözlem için ve küçük kapsamla.
- Testler kaldırılmaz. Somut regresyon + commit öncesi zorunlu kapı;
  kaynak değişmeden kapıyı tekrar tekrar çalıştırma veya yeni test havuzu
  açma yok. Güvenlik invariants, tek scheduler ve quarantine korunur.
- Başlamadan süre hesabı: baseline + model yükleme/kuyruk + öneriler/
  teyit + finalization/cleanup. Mevcut run bütçesi değiştirilmez; yetersizse
  kapanışı doğrulanmış eski koşudan sonra yeni finite admission yapılır.
- Uzun baseline öncesi provider hareketi, çıktı JSON/AST sözleşmesi,
  veri uygunluğu ve sürüm uyumunu ucuz ön kontrolden geçir. Ön kontrol
  başarılı gerçek model araştırması yerine sunulmaz.
- Bir gerçek başarısızlıkta tek neden ve tek düzeltme seç; gözlem aracı
  hatasını ürün hatasıyla karıştırma. Aynı engelde kanıtsız tekrar koşma.
- UI her aşamada actual run/phase/son ilerleme zamanı/engel/raporu gösterir;
  bilinmeyen durum açıkça unknown/unresolved olur. Yalnız test sayısı veya
  commit sayısı ilerleme ölçüsü değildir.
- Lisans ve genel CI, GPU kabulünden ayrı iş listesindedir. AOS'a yazma,
  diğer oturum süreçlerini durdurma, push/merge/deploy yetkisi çıkarılmaz.

Goal kullanıcı isteğiyle duraklatıldı. Bu plan yeni uygulamayı otomatik
başlatmaz; kullanıcı devam dediğinde önce sıra0 uygulanır.

## Uygulama başladı — 2026-10-01

Kullanıcı planı uygulama goal'ını başlattı. Önceki paused kaydı tarihsel
inceleme durumudur; mevcut goal active.

Eski araştırmanın container'ı silinmemiş, exited/PID0 durumundaydı.
Exact ID/owner label ve bütçeler doğrulanarak yalnız readback için açıldı;
random loopback port artık32768. Ana/diğer oturum kayıtları değiştirilmedi.
Production recovery inspect eski boot sahibini proven-dead, aktif job
listesini boş,11 skoru completed buldu. Yetkili API stop_requested verdi;
mevcut baseline reconcile yarım experiment'i abandoned yaptı.

İki recovery stop SQL'i statement fence ile uyumsuzdu: sıfır satır
UPDATE bile P0001 üretiyordu. Her ikisi, exact kayıtlı owner_id/origin
ile mevcut `lab.request_director_run_stop` çağrısına çevrildi. Lease/death,
nesil, drain, SQL guard ve quarantine kuralları korunur. Gerçek retry
artık exception yerine pending döndürdü; özgün stopped-closure cleanup
deadline dolduğu için **terminal rapor/GPU release kabulü yok**.
Eski11 skor ve unresolved kayıt korunur; süre uzatılmadı.

Odaklı12 kontrol exit0. Zorunlu kapı2125 passed/7 skipped/120 GPU-live
deselected, yedi komut exit0,201,171288937s ve source_unchanged=true.
Private log `delivery-recovery-gate-89b67e7161ba4875bc09f82a905fb1be.log`.

Ana API/Director-drain/console, mevcut `ops/start-lab.sh` ile absent
durumundan açıldı; canlı servis restart edilmedi. Arayüz API bağlı.
Tailscale köprüsüHOST:8788 üzerinde yalnız aserdargun
HOST cihazını kabul ediyor. Köprü aktif/listening; uzaktaki SSH
portu reddettiği için remote browser erişimi bu oturumda doğrulanmadı.

Yeni bağımsız gerçek araştırma için mevcut ana ledger'ın dört sentetik
profili read-only Planner ile doğrulandı. Ayrı registry/principal, ana
UI sahibini korur;6 öneri/7200s/180000token immutable bütçesi hazırlanır.
İlk helper principal adı hatası ve iki tokenizer timeout'u saklandı.
Gerçek offline tokenizer diagnostic16 tokenı3,028164879s'de saydı.
Tam altı bağlam/JSON/AST ön kontrolü önce3GiB sonra gerçek dispatch'in
2GiB/no-swap/100%CPU profilinde geçti (717–720 input token).
Tokenizer/provider kaynak kodu veya10s sınırı değiştirilmedi.
Bu **ön kontrol**, model önerisi veya GPU kabulü değildir.

Recovery teslimi commit `62259ff`. Yeni gerçek araştırma
`f6b2382f-21a2-452c-8323-a4c9eede7b1d` mevcut ana ledger'da running;
run-bound Director InvocationID `cd0b958edee141d79f9551aa1a45df11`.
Ana API kimliğiyle readback ve console watch listesi doğrulandı.
Bu teslim gözleminde5 bağımsız baseline skoru tamamlanmış, aday sayısı0;
model araştırması/terminal rapor henüz kabul edilmedi.
Kendi sınırlı progress monitor'ü20s aralıkla read-only ledger'dan arayüz
durumunu günceller. Ana kaynaklar koşu boyunca değiştirilmez.
Eski readback PG container'ı exact ID/label/image doğrulaması sonrası
exited/PID0 oldu; silinmedi, eski unresolved ledger korunuyor.

### AOS oturumuna güncel aktarım (salt okunur kontrol)

- Scientist `62259ff`, AOS `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
  Gerçek araştırma hâlen çalışırken entegre GPU koşusu başlatılmayacak.
- Actual Decider/Bonsai `services/*/broker_worker.py` mevcut; hash'ler
  `af04fab479d9d5326fd5456dd1592c9a444ee478831b50ef9070ee0b73d4cb8a` ve
  `827b3fc438a0666a903da3862d85c8348b546c757431ee8b38d48a80aba72dab`.
- `shared-gpu-turns`/`lab-external` bayrakları actual kaynak taramasında
  bulunmadı. `scripts/serve_desktop.py` native Scientist factory/admission/
  output-contract/current-runtime hook'larını kullanıyor. Eski bayraklı
  izole test kopyası actual ana dal kabulü yerine geçmeyecek.
- Actual infer wire request/receipt version1; native bootstrap
  `aos-scientist-control.v1`. Lab'ın ayrı broker kontrol sözleşmesi
  `aos-scientist-control-contract.v2` (capability/status/cancel/reconcile).
  Bunlar ayrı katmanlardır; aynı isimli/sürümlü dış arayüz oldukları
  varsayılmayacak. Native factory adaptörünün mevcut açık hook'larıyla
  aynı exact deployment/source/capability kimliği üzerinde anlaşılmalı.
- AOS oturumu, kendi runtime'ında Scientist-owned workspace desteğini
  veya AOS tarafından hazırlanmış ayrı gerçek workspace/runtime girişini
  sağlamalı. Scientist AOS dizinine yazmayacak. Dummy desktop/task/pin
  veya yalnız idle/quiesce ile kabul/release yapılmayacak.
- Tek canonical DB `~/.local/state/swapp-gpu/arbiter.sqlite3` korunur.
  Entegre GPU kabulünün tek yürütücüsü Scientist; owner/generation,
  cancellation→drain→release/quarantine, timeout ve sürüm/capability
  reddi actual uçtan uca koşudan önce teyit edilmelidir.

Çalışan araştırmanın yapılandırılmış modeli Qwen3.5-9B,
revision`c202236235762e1c871ad0ccb60c8ee5ba337b9a`, FP8 per-tensor.
S1 context/output8192/2048, model-max-len10240; S2 context/output8192/8192,
model-max-len16384 ve thinking budget512. Bunlar yapılandırma değerleridir;
actual VRAM/startup/inference/drain ölçümleri model aşaması tamamlandığında
native receipt'lerden alınacaktır. 16k/32k×2 kapasite kabulü verilmez.

## İlk gerçek model koşusunun sonucu — düzeltme için kanıt

Run `f6b2382f-21a2-452c-8323-a4c9eede7b1d`, Scientist
`62259ff1a0942212bc7e1611d9b18862798b9dc9`:36/36 CPU baseline,
6/6 SHA doğrulanmış gerçek Qwen önerisi; aday Scorer ölçümü0.
Altı terminal neden sırasıyla candidate_crash, candidate_crash,
degenerate_constant_scores, score_length_or_index_mismatch,
candidate_crash, causality. Model çıktıları gerçek olsa da başarılı
araştırma veya kabul değildir. Loop proposal_limit_reached; ledger failed,
report_sha256 null, reason finalizer:did_not_terminalize.

İki kök neden: adaylar çok sütunlu DataFrame/fitted-state/tek boyutlu
causal skor sözleşmesini yanlış kullanmış; yeni suite için ayrı holdout
kaydı kurulmamış. Yalnız mode-grid finalizer'ı bu araştırmayı kapatamaz.
Guard/Scorer veya terminal fencing gevşetilmez; prompttaki çalışan kontrat
örneği ve yeni admission öncesi bağımsız holdout kurulumu düzeltilir.
Özgün failed run ve bütçesi değiştirilmez.

Parent/Director inactive/MainPID0. Canonical arbiter altı lab isteğini
done, active_owner null gösterdi. Altı özgün model MainPID/GPU PID ve
cgroup path yokluğu ayrıca gözlendi; ilgili unit'ler inactive/MainPID0.
Private cleanup readback SHA256
`71aa4e3f96abdcf84a623dcd704857a784ec14d5b9de1c3f00d1503c36ee3dee`.
Bu koşunun cleanup gözlemi, eski unresolved koşuyu veya AOS kabulünü
kapatmaz. VRAM tepe12898MiB; parent wall1868,25223975s. Mevcut
Qwen3.5-9B/fp8_per_tensor kullanıldı; yeni model indirilmedi. S1/S2
context/output ve diğer gecikmeler özgün receipt'lerden ayrıca alınmalı.

UI üst paneline yapılanlar/şu anki engel/kalan teslimler, bağımsız aday
puan sayısı ve açık failed başlığı eklendi. Production frontend build exit0;
gerçek tarayıcı JavaScript hata0,390px mobil yatay taşmafalse.

SHA doğrulanmış altı proposal receipt: S1×2/S2×4; toplam11720
model input+output token. Startup75,638–78,462s, inference11,569–25,902s,
drain0,460–0,762s. Bunlar model çağrılarının ölçülen aralıklarıdır;
AOS sıra bekleme/devir gecikmesi ölçülmedi. S1 output limit2048,
S2 limit8192; bounded araştırma profilleri tam kapasite kabulü değildir.

Yeni private hazırlık dev manifest
`a518fb3e74cafa5677eee216466c6e97bf06e51a194fd45955c1607ca24b0838`
ve ayrı held-out manifest
`b46205959a1f145c6b8fb54dfa6b3be10bc8fa4dfa99d20005ecb0b40220b998`
ile kuruldu. Migrator yalnız yeni profiler/labels; Scorer dört holdout
görevini kaydetti ve exact bağımsız readback geçti. Altı offline context
1141/1141/1138/1139/1138/1138 token; yeni provider config
`2177cc74fa94930fe33e4ed1855eaaf3b0321bedcdedb2e07222d6b8614338fd`.
Holdout normalizasyonu açık identity VUS0/1 endpoint'idir; ölçülmüş
baseline calibration değildir. Public veri/araştırma kabulü değildir.

Prompt v6 çok sensörlü fit/score çalışan örneğiyle güncellendi; tarihsel
v2–v5 receipt desteği korunur.25 odaklı kontrol ve ruff geçti. İlk zorunlu
kapıda yalnız Bandit prompttaki İngilizce select/from metnini SQL sandı;
statik, SQL'e gönderilmeyen bu prompt literal'ına scoped B608 açıklaması
eklendi. Gerçek SQL kontrolleri değiştirilmedi; son kapı ayrıca çalışıyor.

Son zorunlu kapı2126 passed/7 skipped/120 GPU-live deselected; yedi
komut exit0,199,939922081s, source_unchanged=true. Prompt/UI teslimi
sonrası yeni gerçek koşu admission hazır; henüz yeni araştırma kabulü yok.

Yeni gerçek research run `5679dc70-3343-466e-abd0-fde082e67e7d`
`f9c9281` ile yeni authenticated API18592 üzerinde admitted. Runtime
Director active/MainPID142781/invocationc2d6a0cdd37d4badb54580b42bafdd0a;
ledger running.36 baseline ve6 gerçek model önerisi/holdout raporu sırada.
UI watch eklendi; read-only monitor20s güncelliyor. Başlatılmış olması
araştırma veya AOS kabulü değildir. AOS checkout değiştirilmedi.

## Kullanıcının AOS ve16GB VRAM önceliği — 2026-10-01

Son kullanıcı yönlendirmesi teslim sırasını günceller; özgün kabul ve goal
kapsamını kaldırmaz. İlk gerçek bağımsız research/rapor ve güvenli iptalden
sonra **actual AOS görevi + LSH/OPTICS/SOM/anomali agent deneyleri** birlikte
öncelik kazanır. Daha sonra dört public kaynak/holdout, genel model/skill/
eğitim ve açık çekirdek dağıtımı ayrı kabulleriyle sürer.

Hedef eylemci: AOS'tan bütçeli araştırma görevi/snapshot alır; yalnız normal
eğitimden mod ve tolerans öğrenir; yöntem/hiperparametre önerisini güvenilir
compiler üzerinden Docker deneyine taşır; bağımsız Scorer/Referee ile
mevcut yönteme karşı ölçer; yalnız kanıtlanan iyileşmeyi benimser. Mod
uzaklığı, SOM uzaklığı, NN tahmini ve OMR ayrı çıktılar olarak korunur.
Sürekli gelişim, sınırsız GPU işi veya doğrulanmamış model eğitimi değildir:
sonlu episode bütçeleri, kayıtlı champion/holdout, restart/rollback ve
izlenebilir karar geçmişi gerekir.

Mevcut sklearn/numpy CPU LSH/OPTICS/SOM/istatistik yolunu kullan; yeni ağır
GPU dependency/model indirme gerektirme.32GB RAM/16GB VRAM host kaynak
profilleri ve tek canonical scheduler korunur. Yerel LLM yalnız sınırlı
çağrı boyunca GPU edinir; çağrı sonrası exact drain/release gerekir. AOS
ve Scientist'in ilerlediği gerçek birlikte çalışma hâlâ kabul bekler;
Scientist-only12898MiB ölçümü bu kabulün yerine geçmez.

Actual AOS source63 seçili SHA
`b56c54a1bb63653f0bc69ad55caf25f8cce353ece97e8d7ceab80a640e03ca46`
ve native exact/bounded SystemdCallerAuthenticator için dar patch hazır:
`484708d5c4ed56acdd6dc18591458dbfaf669bd67407fceb807553e4973c552b`.
Bu patch **uygulanmadı**; source seçimi tam deployment yetkisi değildir.
Bootstrap control.v1/inference1, contract-v2 metadata ve retained evidence-v3
farklı katmanlardır. AOS kendi yetkili ayrı repo/data workspace/runtime'ını
oluşturmalı; Scientist AOS'a yazmaz veya aktif user workspace'ini kullanmaz.

Self-service registry append/holdout readiness/pending config patch'i
`902224f4e63fb214b8dfd372c9074535d4efe58997e4f17d0fdd1d02010de2a9`
hazır; kaynaklara uygulanmadı. Eski owner-null ledger satırları üzerine
işlem veya global bloklama yok; activation mevcut owned service/inflight/
cleanup kanıtıyla ayrı adımdır. Runtime cancellation için exact captured
owner observer yaması ayrıca hazır; ilk çalışan araştırma değiştirilmiyor.

Önceki failed koşunun read-only HTML tanı raporu hash'i
`8a99cdd8356b2c6fb2de43a0dc762d0588fb221a9277202eccb70960ee2c609d`;
altı terminal REJECT mevcut replay ile exit0 doğrulandı. HTML renderer
çıktısı bağımsız Scorer terminal/holdout raporu veya başarılı araştırma
değildir; özgün failed state/report_sha256 null korunur.

## Scientist source63/caller engeli giderildi

AOS oturumunun blocked aktarımına karşı dört Scientist script'ine source63
ve native exact/bounded SystemdCallerAuthenticator bağlantısı uygulandı.
İki lazy API loader güncellendi; eksik native API/yanlış kaynak membership/
migration ve receipt sonrası source drift kabul edilmez.172 odaklı kontrol
ve ruff exit0; source_unchanged=true. Actual seçili AOS63 SHA iki okumada
b56c54a1bb63653f0bc69ad55caf25f8cce353ece97e8d7ceab80a640e03ca46.

Zorunlu kapı yedi komut exit0,202,438947178s ve source_unchanged=true.
Private log aos-source63-delivery-gate-18ea955a49b64c568652b43f5d9031e4.log.
AOS kaynak/proses/DB/policy değiştirilmedi; ortak runtime yapılandırması
ve yetkili ayrı workspace henüz kabul edilmedi. Çalışan standalone
araştırma bu dört script'i import etmiyor; lab/harness kaynakları değişmedi.

Kısa AOS aktarımı: Scientist source63/caller artık hazır. Mevcut source63
selected pin ile current source/config/principal/artifact/dependency
bundle'ı gözden geçirelim; enabled workspace/runtime'ı AOS oturumu sahibi
hazırlamalı. Source_ready, GPU admission veya cleanup yetkisi değildir.
Gerçek GPU kabulünün tek yürütücüsü Scientist; devam eden standalone
koşunun terminal/release kanıtından sonra ortak kabul başlatılabilir.

## Actual source63 ve AOS-owned staging eşlemesi

Actual source63 preflight tam reviewed HEAD/diff/selected/untracked pinleriyle
exit3/source_ready=true; tek neden joint runtime capability pending.
AOS oturumu ayrı owner-only workspace ve kurulmamış GPU-disabled unit
hazırladı. Scientist preparer bu unit adına yeni disabled policy/config'i
exit0 ile üretti. Hash'ler ve dört native factory hook bağlantısı
[104 numaralı aktarımda](104-aos-source63-runtime-handoff.md) kayıtlıdır.
Kaynak kabulü gerçek runtime/GPU kabulü değildir; enabled policy ve gerçek
artifact/current-rights/generation composition hâlâ tamamlanmalıdır.

İkinci actual standalone koşu `5679dc70-3343-466e-abd0-fde082e67e7d`
36 baseline/6 gerçek model önerisi/24 candidate Scorer ölçümüyle failed
kapandı. Holdout worker baseline source'u Scorer candidate-blobs'ta
bulamadı; ardından failed application kapanışının exact reconciled budget
kontrolü ProgrammingError verdi. Başarılı araştırma/holdout raporu yoktur.
Admission kaynak commit'i f9c9281; parent'ın kapanış anında okuduğu
44f840b başlangıç kaynağı diye sunulmaz. Özgün failed run değiştirilmez.
Bu iki somut hata için dar düzeltme hazırlanıyor; UI durum metni güncellendi.

Bu koşunun fiziksel cleanup readback'i ayrıca doğrulandı: canonical active
owner null,6 request done;6 model unit inactive/not-found/MainPID0; original
PID ve GPU PID yolları ile cgroup'lar yok. Yalnız KWin1298/12MiB kaldı;
kullanıcı işi kesilmedi. Private terminal-gpu-readback.private.json hash'i
`eea6c985cb1f78fa0307e5b3b1e2d3d2c437d474edfe432cde4499914e7f7282`.
Bu cleanup araştırma/holdout başarısızlığını başarılıya dönüştürmez.

SHA-doğrulanmış altı provider receipt ve canonical runtime binding ölçümü
private runtime-measurements.private.json içine kaydedildi. Qwen3.5-9B
fp8_per_tensor, S1 context8192/output2048/modelmax10240; S2 context8192/
output8192/modelmax16384. Peak VRAM12980MiB; startup74.746–78.298s,
inference11.923–22.893s, drain0.487–1.449s. Queue/devir gecikmesi ayrı
ölçülmedi; wall'dan çıkarılıp gerçek AOS devir ölçümü diye sunulmaz.
Altı independent experiment receipt DISCARD: ilk beş large per-task
regression, sonuncu delta0/ci_low0 no meaningful improvement. KEEP veya
öğrenilmiş iyileşme yoktur.

Dar holdout düzeltmesi uygulandı: yalnız holdout-enabled başlangıç ve
promoted kaynak aynı SHA ile mevcut bounded opaque Scorer artifact store'a
yazılır; failed/bitless run-end reconciliation mevcut owner-bound closure
yolunu kullanır. SQL owner/generation/budget şartları değiştirilmedi.
Eski kaynakta iki regresyon başarısız, passed-bit kontrolü başarılıydı;
düzeltmede3/3 geçti.42 ilgili kontrol ve ek13 fixture kontrolü geçti.
İlk tam kapıda EXPLORE fixture eksik holdout_enabled ayarı ve16GB /tmp
tmpfs'in20GiB disk rezervini sağlayamaması görüldü. Fixture açıkça inert
holdout-disabled oldu; kapı Scientist-owned NVMe TMPDIR'de yeniden çalışıyor.
Production rezervi veya güvenlik kontrolü gevşetilmedi. Yeni bağımsız
tek-öneri koşusu hazırlanmıştır; henüz admitted/model/GPU kabulü yoktur.

Son zorunlu kapı **2136 passed/7 skipped/120 GPU-live deselected**, yedi
komut exit0; parent187.127715298s/source_unchanged=true. Private log
holdout-finalization-fix-stage/quality-gate-delivery.private.log. Kısa NVMe
pytest basetemp socket sınırını da korur; README komutu buna göre güncellendi.
Eski failed ledger/rapor/bütçe kayıtları değiştirilmedi. Push/merge/deploy yok.

Yeni bağımsız run `9564b284-d6f8-4297-9f62-1a58383536bd` be94b60
kaynağıyla authenticated API18592 üzerinden admitted;1 öneri/30000 model
token/7200s immutable bütçe. Actual Director active/MainPID320015/
invocation8ffcb16259de4349abe03772a168f612; başlangıç CPU ölçümleri sürüyor.
Canonical pre-admission active owner null ve yalnız KWin GPU12MiB görüldü;
bu rezervasyon değildir. GPU çağrısı ancak aynı canonical scheduler'dan
gerçek tahsis alınca yapılır. UI watch HTTP200; read-only monitor20s.
Terminal/holdout/rapor kabulü henüz yoktur. Parent admission ve result-time
source commit'lerini ayrı kaydeder. AOS joint test çalıştırılmadı.


2026-10-01 güncellemesi: `9564b284-d6f8-4297-9f62-1a58383536bd`
terminal raporlu tek gerçek S1 araştırması tamamlandı;36baseline+4aday,
model adayı DISCARD, baseline holdout passed, cleanup doğrulandı.
[Doğrulanmış teslim ve açık kabuller](105-first-completed-local-research.md).
M0.13, OM ve AOS toplam kabulleri bu sentetik tek-öneri koşusuyla kapanmaz.

Bu kaynak tesliminde structured operating-mode model/agent API/UI, owner-bound
responsive model cancellation ve authenticated AOS capability eklendi.
Son mandatory gate2199 passed/7 skipped/120 GPU-live deselected,7exit0.
Gerçek inflight stop ve AOS/structured model kabulü hâlâ açık.

## 0.45.0 somut CPU teslimi — 2026-10-03

Gerçek arayüzden SKAB DEV çalışması tamamlandı: 283,41 saniye, 9 baseline +
3 OPTICS skoru; LSH guard reddi. OPTICS KEEP ham VUS-PR üstünlüğü değildir.
OMR/sensör görünümü ve sahipli süreç kapanışı doğrulandı. AOS gerçek GPU,
holdout, öğretmen eğitimi ve otomatik öğrenilmiş iyileşme açık kalır.
[Adımlar ve ölçüm kanıtı](120-public-dev-cpu-study.md).

## 0.46.0 saha amacı teslimi — 2026-10-03

Varlık/hedef → seçilmiş snapshot → gerçek CPU deneyi → doğrulanmış bağlam
görünümü tamamlandı. 279,61 saniye, 12 skor, idempotent retry ve cleanup
kanıtlandı; model faydası, saha kimliği ve AOS GPU kabulü açık.
[121: kullanım ve kanıt](121-field-intent-workflow.md).

Kullanıcı ek talebi: başlangıçtan bugüne token/maliyet kapsam raporu ve saatlik
güvenli commit/push. Bu yetki önceki push yasağını yalnız incelenmiş güvenli
public değişiklikler için günceller; özel geliştirme geçmişi yayımlanmaz.
Merge/deploy otomatik açılmaz. Ana araştırma goal kapsamı korunur.
