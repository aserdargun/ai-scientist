# Paralel AOS geliştirmesi için entegrasyon notu

Tarih: 2026-09-24. Kullanıcı AOS üzerinde başka geliştirme oturumunun paralel çalıştığını bildirdi ve AOS değişikliği gereksiniminin paylaşılmasını istedi. Bu not o oturuma iletilebilir. AI Scientist uygulama ajanı mevcut AOS kaynaklarını şimdilik değiştirmiyor; değişiklikler ayrı yama/opt-in test alanında hazırlanacak.

Amaç: mevcut CachyOS, RTX 4070 Ti SUPER 16 GB VRAM ve 32 GB RAM host üzerinde AOS kullanılmaya devam ederken AI Scientist uzun araştırma koşularını ilerletebilmeli. Aynı anda bütün modellerin VRAM'de tutulacağı varsayılmıyor. Bağımlılık, port, süreç, RAM ve GPU çatışması engellenmeli.

## Güncel izole rebase — 2026-09-29

Canlı AOS'tan 1082 public dosya, 11.185.537 baytlık yeni kaynak kopyası
alındı; kopyalama boyunca kaynaklar sabit ve byte eşliği doğrulandı.
Snapshot yolundaki `20260927` eski sürücünün adıdır; gerçek kayıt tarihi
2026-09-29'dur. İki mevcut manifest farkı (`check_capabilities.py` ve testi)
korundu; yalnız ayrı merged kopyanın manifesti yeniden üretildi.

Güncel retention/readiness/pin davranışı korunarak typed Lab araçları,
ayrı dış iş kuyruğu, opt-in ortak GPU broker, bootstrap ve beklemeler
sonrası session/lease/generation kontrolleri 31 dosyalık yamada uzlaştırıldı.
Güncel migration zinciri 0017 ile biter; yeni 0018/0019 kopyada çakışmaz.
**Session 37161 / exit 0:** 109 hedefli test geçti, gerçek pinned Chromium
gerektiren bir test atlandı; paket kapısı **4360 kontrol / exit 0**.
Yama check/apply exit 0 ve ayrı temiz kopyada tam byte eşliği doğrulandı.

[Güncel kaynak kaydı](review-evidence/aos-current-rebase-preparation-0101ef6c806c.json),
[izole teslim](review-evidence/aos-current-rebase-20260929-receipt.json),
[güncel kaynağa bağlı yama](review-evidence/aos-current-lab-broker-20260929.patch).
Canlı AOS'a uygulanmadı. GPU/model/gerçek birlikte çalışma başlatılmadı;
bekleyen GPU koordinasyon yanıtı ve gerçek birlikte ilerleme kabulü korunur.
İlk git metadata envanteri preflight hatası özel kanıtta tutulur;
o denemede test çalışmadı. Bu teslim CPU kaynak/paket doğrulamasıdır.

## Salt okunur yeniden kontrol — 2026-09-27

On iki entegrasyon temas dosyasının **yedisi**, 2026-09-24 18:50 UTC
görüntüsünden farklı. İsimle yapılan sınırlı aramada `src/aos`, `services`
ve `scripts/serve_desktop.py` altında `ai_scientist`, `lab.start/status`
ve ortak GPU adapter adları bulunmadı; bu, farklı adla uygulanmış bir
bağlantıyı dışlamaz. Koordinasyon dosyası adı araması yalnız mevcut devir
notunu buldu; yanıt veya yeni izin doğrulanmadı.

AOS dizininde erişilebilir Git HEAD yok; gelecekteki uzlaştırma dosya
hash'leriyle yapılmalı. Eski izole yamalar güncel canlı kaynağa doğrudan
uygulanamaz. Bu kontrolde AOS dosyası, servis, model veya DB değiştirilmedi.
Kaynak hash'leri, komutlar ve gerçek çıkış kodları:
[salt okunur kontrol kaydı](review-evidence/aos-integration-readonly-20260927.json).
Bekleyen GPU koordinasyonu sürerken Lab'ın CPU geliştirmesi devam ediyor.

### Güncel kaynak kopyası ve yama kontrolü

`data/runtime/aos-coexistence/source-rebase-20260927-0a9e16023f95`
altına 937 public kaynak dosyası (9.586.870 bayt) kopyalandı. Kaynaklar ve
dosya envanteri kopyalama boyunca sabit kaldı; kopya byte eşliği doğrulandı.
928 dosyalık mevcut manifestin beş hash farkı ve manifest dışındaki dokuz
public dosya ayrı kaydedildi; bu bir paket doğrulama sonucu değildir.

Tarihsel `aos-lab-gpu-broker.patch` için **uygulamadan yapılan
`git apply --check` exit 1** verdi. Beş mevcut dosyada hunk eşleşmiyor:
`scripts/serve_desktop.py`, `services/decider/worker.py`,
`src/aos/desktop_console.py`, `src/aos/reusable_decider.py` ve
`src/aos/supervisor.py`. Yamanın yeni dosya olarak eklediği
`MANIFEST.sha256` ile `services/laya/worker.py` de güncel kopyada zaten var.
Kontrol kopyadaki kaynakları değiştirmedi. Eski manifesti veya güncel
dosyaları silerek bu farklar giderilmiş sayılmaz; entegrasyon güncel
kaynak davranışı korunarak yeniden hazırlanmalı.

[Kaynak ve kontrol kaydı](review-evidence/aos-current-rebase-preparation-0a9e16023f95.json)
937 dosyanın hash'ini, komutu ve gerçek hata çıktısını içerir;
[kopyalama sürücüsü](review-evidence/aos-current-rebase-snapshot-driver.py)
aynı SHA ile arşivlendi. Hazırlık sürücüsünün exit 0 sonucu yalnız kopya ve
kontrol kaydının üretildiğini gösterir; yamanın başarıyla uygulandığı
anlamına gelmez. Canlı AOS kaynağı veya çalışma durumu değiştirilmedi,
servis/model/DB başlatılmadı. Gerçek birlikte çalışma kabulü açıktır.

Astra/high bağımsız kaynak incelemesinde 937 hash'i yeniden doğruladı.
Mevcut `services/laya/worker.py`, eski yamadaki eklenen dosyayla byte eş;
yeniden eklenmesine gerek yok. Manifest hunk'ı yerine birleşik test
kopyasının manifesti son kaynaklar üzerinden üretilecek. Güncel kaynakta
ana migration zinciri `0017` ile bitiyor; yamanın `0018/0019` adları teslim
öncesinde paralel geliştirmeyle tekrar kontrol edilmeli.

Uzlaştırmada korunacak güncel davranışlar:

- `serve_desktop.py` GPU retention seçenekleri ve mevcut lifespan:
  ortak broker opt-in olduğunda CPU prewarm/GPU retention çakışması
  reddedilmeli; mevcut kapanış, scheduler ve pin prewarm adımları korunmalı.
- Decider worker'ın `prepare_idle_gpu/admit_job_gpu` kontrolleri ve
  `ReusableDeciderEngine` idle/prewarm teardown yolları korunmalı;
  broker'a özel readiness bunları atlamamalı.
- `BonsaiSupervisor.plan` ortak broker yolu, güncel `verify_pins_for_call`
  kontrolünden sonra gelmeli; pin cache/prewarm ve çağrı metrikleri korunmalı.
- Desktop kontrolündeki restart/planning/sequence kilitleri korunmalı.
  Lab handler'ları uzun araştırmayı foreground slotuna bağlamamalı;
  beklemelerden sonra session/lease/generation yeniden doğrulanmalı.
  Restart/quiesce sırasında çözülmemiş Lab işleri yeni admission'ı kapatmalı.
- Tam GPU yaması typed/API/storage değişikliklerini zaten içeriyor;
  eski typed-policy yaması tekrar uygulanmamalı. Sonraki lifecycle ve
  control-fence yamalarının davranışları güncel kaynağa ayrıca taşınmalı.

## Son koordinasyon gereksinimi — 2026-09-25

0.25 gerçek Qwen öneri denemesi sırasında canlı AOS'un
`models/bonsai-runtime-prism-b10709-9a9394a/llama-prism-b10709-9a9394a/llama-server`
süreci ortak kuyruğa katılmadan GPU kullandı. Süreç `session-28.scope`
altındaydı; Lab gözlemcisi yalnız kendi test süreçlerini kapattı.
Session 54140 / exit 1 sonucunda geçerli öneri veya birlikte çalışma
kabulü oluşmadı. Kanıt: `review-evidence/local-qwen-provider-025-outcome.json`.

Gerçek kabul denemelerini ilerletmek için paralel AOS oturumunun model
başlatma yolları ortak broker'a bağlanmalı veya ayrı, kısa bir GPU test
aralığı koordine edilmeli. Bu seçim kullanıcıya iletildi; henüz yanıt
kaydedilmedi. Canlı AOS dosyaları ve servisleri değiştirilmedi. Ayrı test
kopyasının lifecycle/control yamaları hazır; desktop ve araştırma
birlikte test sürücüsünün ikinci sürümü hazırlanıyor.

Lab broker doğrulayıcısının dar ek yaması, sonradan oluşan canonical
`swapp-ai-scientist-director-dispatch-<run UUID>.service` üst sürecini
yalnız Lab sahibi için kabul eder; PID/başlangıç/boot/InvocationID/cgroup
eşliği zorunludur. Sabit owner çözümlemesi ve AOS ad alanı korunur.
Hedefli 27 test geçti; yama ve kaynak bağları
`review-evidence/lab-runbound-lab-principal-025-main-source.json` içindedir.
Bu CPU doğrulaması gerçek AOS araştırma kabulü değildir.

Yeni izole teslim (2026-09-25): `review-evidence/aos-lab-optin-lifecycle.patch`
normal desktop bootstrap'a açık Lab seçenekleri, model çağrısı sonrası güncel
yetki kontrolü, eşzamanlı aynı-key start birleştirmesi ve restart/takeover
drain davranışı ekler. Yama frozen `c33325349e856edf52d1ab37da394388eb0607c2`
kopyası içindir; canlı AOS'a doğrudan uygulanacak yama değildir.
Dokuz dosyanın base/head hash eşliği ve bağımsız `git apply --check` geçti.
24 hedefli test ve 4100 paket kontrolü exit 0; geniş tarayıcı testinde
host'a özgü `browser-manifest.json` eksik olduğundan 12 test başarısızdır.
Tam Ruff mevcut 206 tanıyla exit 1; değişen satırlarda tanı bulunmadı.
Kanıtlar `aos-lab-optin-lifecycle-review.json` ve
`aos-lab-optin-lifecycle-root-review.json`. Bu dilimde model veya GPU
çalıştırılmadı; gerçek araştırma/desktop birlikte kabulü açıktır.

Ardışık ek yama `aos-lab-optin-control-fence.patch`, control policy await'i
sonrasında session/binding/typed state'i yeniden doğrular; stop için güncel
HUMAN lease/generation/runtime de eşleşmelidir. Kısa deadline bu kontrolden
sonra başlar. Status/report deterministik host policy kullanır, polling
LLM açmaz. İki yamanın sırayla uygulanması ve son üç dosyanın byte eşliği
root tarafından doğrulandı. 27 hedefli + restart API testi ve 4100 paket
kontrolü exit 0; gerçek model/desktop kabulü açık. Kanıt
`aos-lab-optin-control-fence-review.json` ve `...-root-review.json`.

Güncel sonuç (2026-09-25, Lab 0.20): izole kopyada gerçek
**Qwen S1 → Decider → Qwen S2 → Bonsai** çağrılarının dördü de geçti.
Session 52269 / exit 0, 12/12; 211,187 sn. GPU zirve 12754 MiB ve son
62 MiB; en düşük kullanılabilir RAM 18,4 GiB. Kaynaklar sabit ve bütün
test süreçleri kapandı. Aritmetik/sentetik girdiler kullanıldı; tam
araştırma ve desktop kabulü açık. Canlı AOS değiştirilmedi.
Kanıt: `aos-qwen-020-broker-model-outcome.json`.

Sonraki Lab 0.21 tek S2 araştırma probe'unda, ortak kuyruğa katılmayan
`/home/cachyos/.venv/bin/python` süreci `session-26.scope` içinde 1180 MiB
GPU belleğiyle gözlendi (session 20177 / exit 1). Lab yalnız kendi test
birimlerini durdurdu; dış süreç korunup AOS'a atfedilmedi. Kaynak dosyalar
değişmedi. Paralel AOS oturumu model denemesi yapıyorsa gerçek birlikte
kullanım için model yükleme/çıkarım yollarının da opt-in ortak broker'ı
kullanması gerekiyor. Sadece Lab'ın sıraya katılması yeterli değil.
Kanıt: `local-qwen-compact-021-s2-review.json`.

Önceki durum (2026-09-25): izole güncel AOS kopyasında gerçek Decider ve Bonsai
recovery çağrıları ortak GPU aracısından geçti; iki model süreci çağrı
sonunda kapandı (8/8, session 38880 / exit 0, GPU 62→8816→62 MiB).
Ek dosyalar `services/decider/broker_worker.py` ve
`services/bonsai/broker_worker.py`; mevcut `services/decider/worker.py`
için GPU hazırlık sınırı ve istemci metrikleri değişikliği de gerekiyor.
Bu kaynaklar yalnız test kopyasında. Canlı AOS değişmedi.
Sonraki birleşik tanı denemesinde gerçek sıra Lab → AOS → Lab → AOS oldu;
S1/Decider/Bonsai geçti, S2 512 token sınırına takıldı (10/11 kontrol,
session 74499 / exit 1). GPU süreçleri temizlendi, son bellek 62 MiB.
Bu nedenle tam Qwen araştırması ve gerçek desktop kabulü açık kalır.
Lab broker'ı 0.19.0 kalıcı paketine alındı; 231 testli tam kalite kapısı ve
imaj byte eşliği geçti. `20-aos-broker-model-review.md` kapsamı kaydeder.

Güncel teslim `review-evidence/aos-lab-gpu-broker.patch`: kayıtlı baseline
manifest `fb0c38f2…` üzerine 24 dosyalık tam yama; eski yamalarla zincirlenmez.
Bağımsız uygulama/byte eşliği, 57 hedefli test (9 tarayıcı testi atlandı)
ve 4100 paket kontrolü geçti. Bu yama mevcut canlı dosyalara uygulanmadan
önce paralel değişikliklerle yeniden uzlaştırılmalıdır.

## AOS tarafında gereken dar değişiklikler

Alan | Muhtemel dosyalar | Gerekli davranış
--- | --- | ---
Ortak GPU kaynağı | `src/aos/decision.py`, `src/aos/reusable_decider.py`, `src/aos/supervisor.py`; gerekirse `services/decider/worker.py` | Opt-in shared lease adapter. CUDA/model yüklemeden önce al, kendi GPU süreci drain/unload olmadan bırakma. CPU prewarm ayrı RAM rezerviyle devam edebilir. Reusable Decider normal yanıtta VRAM tutuyor; sadece mutex yeterli değil.
Vision kapsamı | `src/aos/vision.py` | `BonsaiVisionSupervisor` base supervisor yürütmesini kullanıyor; ortak hook'un vision ve recovery'yi kapsadığı doğrulanmalı.
Typed uzman servis | `src/aos/contracts.py`, yeni `src/aos/ai_scientist*.py`, `src/aos/computer.py` | Sınırlı `ai_scientist` task/`lab.start`, `lab.status`, `lab.stop`, `lab.report` eylemleri; arbitrary URL veya shell yok. State, policy, caller identity, bütçe, idempotency ve doğrulama korunur.
Arka plan iş takibi | `src/aos/desktop_tasks.py`, append-only DB migration/yeni external-job repository | Lab run id'sini durable kaydet; uzun Lab koşusu foreground DesktopScheduler slotunu veya input lease'i işgal etmesin. AOS aynı anda başka yetkili kullanıcı görevini yürütebilsin. Start ACK araştırma başarısı sayılmaz; terminal rapor ayrı doğrulanır.
İsteğe bağlı UI/katalog | `src/aos/desktop_console.py`, ilgili task catalog/schemas | Önce API/typed yürütme doğrulanır; kullanıcıya başlatılmış/devam ediyor/tamamlandı durumları açık ayrılır. Türkçe serbest görev normalizer'ı ve birleşik planlar, mevcut dar katalog kanıtını kaybetmeden sonra genişletilir.

## Sınır ve birlikte doğrulama

- Bu projenin Lab API/Qwen/Scorer/Postgres bağımlılıkları AOS venv'ine kurulmaz. AOS'a taşınacak adapter hafif ve transport sözleşmeli olmalı.
- AOS SQLite ve Lab Postgres ortak tablo yazmaz. Cross-reference: AOS task/run/action ↔ Lab run id + request digest + rapor hash.
- Foreground görev bitişi ile bağımsız araştırma işi bitişi karıştırılmaz. Başlatma eylemi, kalıcı Lab handle ve bütçe doğrulamasıyla doğrulanabilir; araştırma işi ancak terminal raporla tamamlanır. Geri plan araştırması kendi sınırlı yetkisine sahiptir; sonraki foreground göreve eski approval taşınmaz. Targeted stop davranışı açık olmalı.
- Lab'ın vLLM'i GPU'yu koşu boyunca tekeline alamaz. AOS interaktif talebi ve Lab talebi sonlu inference dilimleri, unload ve adil kuyruk üzerinden ilerler. Mevcut AOS canlı oturumu koordinasyon olmadan değiştirilmez/kapatılmaz.
- İlk test ayrı opt-in AOS instance'ı ve ayrı DB/port/workspace ile; gerçek modellerde resource transfer, kontrol API yanıtı ve her iki işin ilerlemesi ölçülür. Fixture testi GPU kapasitesi kabulü değildir.
- Mevcut AOS kaynakları paralel değişiyor. [İncelenen dosyaların hash'leri](review-evidence/aos-source-baseline.json) başlangıç referansıdır, kilit değildir. Yama uygulamadan önce dosyalar yeniden okunmalı; hash farklıysa patch yeniden tabanlanmalı. Kullanıcının/paralel oturumun değişiklikleri geri alınmaz.

Henüz AOS dosyası değiştirilmedi ve mevcut yönetilen AOS oturumu yeniden başlatılmadı. İlgili oturumla doğrudan mesaj kanalı mevcut değil; bu dosya koordinasyon için somut devir metnidir.

Güncelleme 2026-09-24: Kullanıcının yinelenen bilgilendirme talebi üzerine AOS köküne yeni `AI_SCIENTIST_COORDINATION.md` devir notu eklendi. Mevcut AOS dosyaları ve çalışan servisler değiştirilmedi. Diğer oturumun notu okuduğu henüz doğrulanmış değil.

Bu not sonrasında AOS package validation exit 1 verdi: `Manifest hash: tests/test_web_https_relay.py`. İlgili mevcut test ve manifest değiştirilmedi. [Koordinasyon kontrol kaydı](review-evidence/aos-coordination-check.json), kontrolün anlık durumunu ve yeni notun hash'ini tutar; gerçek AOS entegrasyon kabulü sayılmaz.

2026-09-24 09:49 UTC salt okunur yeniden incelemesinde `src/aos/desktop_tasks.py` ve `src/aos/desktop_console.py` başlangıç hash'lerinden farklıydı; diğer on temas dosyası aynıydı. [Kaynak değişim kaydı](review-evidence/aos-source-drift-review.json). Bu durum paralel çalışma olduğunu doğrular; önceki kaynak görüntüsüne dayalı yama doğrudan uygulanmaz. Lab tarafındaki hazırlık AOS dosyalarını değiştirmedi; entegrasyon diliminde güncel dosyalar yeniden okunur.

## Ayrı entegrasyon test kopyası

2026-09-24'te `data/runtime/aos-coexistence/source` altında AOS kaynak kopyası ve kardeş `.venv` altında bağımsız Python 3.12.13 ortamı hazırlandı. Canlı AOS dosyası, DB'si, modeli, token'ı veya oturumu kopyalanmadı/değiştirilmedi; yalnız paket kaynakları alındı. [Kaynak hash kaydı](review-evidence/aos-test-copy-source.json) 663 manifest dosyasını (~5.95 MB) ve manifestte henüz bulunmayan dört public kaynak dosyasını listeler. İlk kopyalama sırasında kaynak değişmedi. Bu görüntü paralel çalışmanın sonradan yaptığı değişiklikleri otomatik içermez.

[Ortam ve temel kontrol kaydı](review-evidence/aos-test-copy-check.json): frozen AOS `uv.lock` ile desktop/dataset/browser Python bağımlılıkları, ayrı validation bağımlılıkları; 29 paket `uv pip check` exit 0. `aos.contracts.REPO_ROOT` test kopyasını gösterir. Reusable Decider ve DesktopScheduler mevcut CPU fixture testleri 35/35 geçti. Tarayıcı, AOS servisi ve GPU modeli başlatılmadı.

İlk package validation, henüz manifestte olmayan retention şeması eksikken hata verdi. Dört eksik public dosya eklendikten sonra gerçek schema/fixture doğrulaması ilerledi; kaynak manifestteki güncel README hash uyuşmazlığı nedeniyle package validation yine exit 1. Kaynak kopyasında 11 manifest hash farkı kaydedildi; orijinal manifest korundu. Bunlar entegrasyon yaması öncesi temel durumdur ve başarı olarak sunulmaz. Yama uygulanınca yalnız test kopyasının yeni paket manifesti ve hedefli kontrolleri üretilir; canlı AOS'a aktarım ayrıca güncel kaynakla uzlaştırılır.

## Güncel kaynak karşılaştırması

2026-09-24 18:50 UTC tarihli salt okunur incelemede, on iki entegrasyon temas dosyasının dördü önceki incelemeden ve ayrı test kopyasından farklıydı: `contracts.py`, `computer.py`, `desktop_tasks.py`, `desktop_console.py`. Exact kontrol zamanı ve SHA değerleri [güncel kaynak kaydında](review-evidence/aos-integration-inputs-current.json) bulunur. Bu, tüm AOS paketinin fark sayısı değildir; yalnız bu on iki dosyanın karşılaştırmasıdır.

Güncel typed görev/araç kataloğu hâlâ Lab eylemlerini içermiyor; `src/aos` ve `schemas` aramasında `ai_scientist`, `lab.start`, `gpu_scheduler` veya ortak GPU lease bağlantısı bulunmadı. Entegrasyonun tamamlandığına dair kanıt yoktur. Paralel kaynaklar, canlı süreçler, veritabanları ve mevcut test kopyası bu kontrolde değiştirilmedi. İzole entegrasyon yaması hazırlanırken bu dört dosya güncel sürüme göre yeniden incelenmelidir; eski kopyanın üzerine kör yama uygulanmaz.

## 2026-09-25 — izole typed yama hazır

`source-typed` test kopyasında `ai_scientist` State/Action, typed `lab.*`
policy, kalıcı dış iş kimliği, açık rebind ve takeover stop/hata kaydı eklendi.
Başlatma karar motoru enjekte edilir; durum/rapor kontrolü deterministik host
policy kullanır. Arka plan Lab işi foreground DesktopScheduler'a eklenmez.

Teslim: `review-evidence/aos-lab-typed-policy.patch` ve dosya hash'lerini içeren
aynı adlı JSON. Yama, tam olarak kayıtlı **source-current test kopyasını**
hedefler; onun manifest'i eski mapping yamasının teslim manifest'inden farklıdır.
Yamalar kör zincirlenmez ve canlı AOS'a doğrudan uygulanmış sayılmaz.
On bir dosyanın geçici Git dizininde byte eşliği doğrulandı. Güncel kopyada
32 hedefli test, 3842 paket kontrolü ve Lab tarafında 176 testli tam gate geçti.

Entegrasyonun mevcut dosya temasları: `computer.py`, `contracts.py`,
`dataset_audit.py`, `desktop_console.py`, `lab_external.py`, `storage.py`,
dataset-audit/Lab testleri ve manifest. Yeni migration
`0019_lab_external_job_rebinding.sql` test kopyasına aittir; paralel AOS
migration numaralarıyla birleştirmeden önce uzlaştırılmalıdır. Eksik public
`services/laya/worker.py` yalnız test kopyasının paketine eklendi.

Normal `serve_desktop.py` opt-in bootstrap'ı, gerçek model/GPU hook'ları,
gerçek in-flight stop/restart ve takeover iş sayısı sınırı açık. Bu kaynaklar
canlı oturumda değiştirilmedi. Güncel inceleme ve kanıt kapsamı:
`16-aos-typed-policy-review.md`.
# GPU sıra paylaşımı güncellemesi — 2026-09-25

Lab 0.15.0, sabit `aos`/`lab` hizmet kimlikleri ve dönüşümlü kuyruk çekirdeğini
uygular. İki gerçek systemd CPU servisiyle 20 dönüşümlü işlem ve 11 kontrol
geçti; gerçek model veya canlı AOS kullanılmadı. Ayrıntı ve sınırlar
`17-gpu-arbitration-review.md` içindedir.

Sonraki dar AOS entegrasyonu `ReusableDeciderEngine`, `BonsaiSupervisor` ve
Bonsai'ye yönlenen vision çağrılarının CUDA yükleme öncesinde bu kuyruğa
katılmasıdır. Normal opt-in bootstrap, fixed principal/unit yapılandırmasını
kurmalı; model veya kullanıcı isteği hizmet kimliğini seçememelidir. Lab
status/report sorguları GPU modeli çağırmaz. Kaynak bırakma için gerçek owned
model sürecinin durması ve VRAM gözlemi ayrıca gerekir.

Bu değişiklikler `data/runtime/aos-coexistence/source-gpu` adlı yeni public
kaynak test kopyasında hazırlanır. Önceki `source-current` ve `source-typed`
kopyaları kabul kanıtları için dondurulmuştur. Canlı `/home/cachyos/aos` üzerinde
bu dilimde değişiklik veya yeniden başlatma yapılmadı; oraya uygulama öncesinde
mevcut paralel geliştirmeyle dosya/migration çakışmaları yeniden uzlaştırılır.

Host'ta kısa süreli dış Python GPU kullanımı gözlendi; kendiliğinden sona erdi.
Tek bir boş-GPU snapshot'ı rezervasyon sayılmaz. Native model denemelerinde
başlangıç ve çalışma sırasındaki tüketici kontrolü, toplam kaynak bütçeleri ve
yalnız sahipli süreçlerin durdurulması zorunludur.

## 2026-09-25 — gerçek GPU denemesinde dış tüketici gözlemi

Gerçek Qwen S2 doctor denemesinin sonunda testin dışındaki `session-19.scope`
içinde bir Python GPU tüketicisi ortaya çıktı. AI Scientist gözlemcisi yalnız
kendi test modelini ve supervisor'ını durdurdu; dış süreç GPU'da kalmaya devam
etti. Kaynak uygulamanın AOS olduğu doğrulanmadığından bu olay AOS'a
atfedilmez. Kanıt: `review-evidence/native-qwen-s2-sampling-profile-review.json`.

Ortak GPU kuyruğuna katılmayan bir süreç varken boş bellek kontrolü tek başına
rezervasyon sağlamıyor. AOS tarafında Decider/Bonsai/vision model yükleme,
çağrı ve unload yollarının ortak yöneticiyi kullanması hâlâ gereken dar
entegrasyondur. Hazırlık izole `source-gpu` kopyasında yapılacak; canlı
kaynakları değiştirmeden önce paralel oturumla bu dosya üzerinden uzlaştırma
gerekiyor. Bu olayda canlı AOS dosyası veya süreci değiştirilmedi. CPU kalite
kontrolleri sürerken yeni GPU denemesi başlatılmadı.

## 2026-09-25 — model tanımları ve güncel kaynak farkı

Salt okunur `review-evidence/aos-gpu-model-metadata-review.json` incelemesi,
mevcut Bonsai manifestinin `prism-ml/Ternary-Bonsai-2-27B-gguf`, PQ2_0 ağırlık
ve Q8_0 vision projector kullandığını kaydeder. Manifestte context 16384,
çıktı 512 ve parallel 1 yer alır. Decider'ın yedi dosyası stat toplamıyla
3.78 GB, Bonsai'nin iki dosyası 7.84 GB'dır. Bunlar dosya boyutlarıdır;
RAM/VRAM kapasitesi veya ağırlık hash doğrulaması değildir. Model, cache,
credential veya özel DB kopyalanmadı; hiçbir AOS/model servisi başlatılmadı.

İncelenen **altı GPU temas dosyasından ikisi** `source-gpu` test kopyasından
farklıdır: `reusable_decider.py` artık CPU prewarm idle sınırını 600 saniyeye
kadar kabul ediyor; `serve_desktop.py` buna ilişkin CLI seçeneği ile yeni form
öğrenme seçenekleri içeriyor. Bu değişiklik CPU hazırlığının ömrüne ilişkindir;
GPU'nun 600 saniye tutulduğuna dair ölçüm değildir. Ortak GPU hook'u
hazırlanırken paralel çalışmanın bu değişiklikleri korunup uzlaştırılmalıdır.
Kaynakların inceleme sırasında değişmediği hash ile kontrol edildi. Canlı
dosyalar ve çalışan oturumlar bu incelemede değiştirilmedi.

## 2026-09-25 — izole GPU istemcisi, protokol taslağı

`source-gpu` içinde `aos/gpu_turn.py`, Decider/Bonsai/vision çağrı bağlantıları
ve `serve_desktop.py --shared-gpu-turns` taslağı hazırlandı. Önceki kopyalanmış
`shared_gpu.py`/`gpu_scheduler.py` yolları bu izole kopyadan kaldırıldı.
Yeni istemci sabit profilli Unix soketi isteği gönderir; kuyruk, model başlatma
ve GPU'nun boşaldığını doğrulama Lab hizmetine ait olacak. Gerçek broker ve
Decider/Bonsai için sabit model çalıştırıcıları henüz uygulanmadı; mevcut
Qwen çalıştırıcısı AOS modellerini çalıştırıyor sayılmaz.

İlk taslakta `SO_PEERCRED` PID/UID alan sırası ve `systemctl --user` ortamı
yanlıştı; gerçek yerel negatif kanıt `aos-gpu-client-initial-defects.json`.
Düzeltmeden sonra `review_aos_gpu_peer.py` **6/6, exit 0**: gerçek kısa
systemd CPU servisi/Unix soketi, doğru servis kimliğiyle istek/yanıt, yanlış
PID/UID reddi ve yalnız sahipli test servisinin temizliği doğrulandı.
`aos-gpu-client-actual-peer-review.json` kaynak hash'ini ve komutları saklar.
Dönen inference/model generation alanları açık fixture'dır; model, GPU
paylaşımı veya birlikte çalışma kabulü değildir.

Bu taslak canlı AOS'a uygulanmadı. `source-gpu` hâlâ önceki kaynak görüntüsünü
temel alır; canlı 600 saniyelik CPU prewarm ve yeni form seçenekleriyle
uzlaştırılmadan dağıtım yaması olarak kullanılamaz. İptal/yeniden bağlanma,
durable istek kimliği, gerçek model süreç sahipliği ve bağımsız drain
doğrulaması da tamamlanmalıdır. `source-current`/`source-typed` korunuyor.

## 2026-09-25 — yerel model ve runtime dosyalarının hash doğrulaması

Bir sonraki salt okunur kontrol, manifestlerin gösterdiği gerçek dosyaları
SHA-256 ile doğruladı: Decider için **7 model + 4 kod dosyası**, Bonsai için
**2 model + 84 runtime dosyası**; toplam **97 dosyanın tamamı eşleşti**.
Dosya kimliği/boyutu/zaman damgaları ve manifestler okuma boyunca sabit kaldı.
Kanıt: `review-evidence/aos-local-model-pin-review.json` ve ayrı supervisor
exit kaydı `aos-local-model-pin-review.launch.json`; ikisi de exit 0.

Kontrol 256 MiB/1 CPU sınırında, düşük CPU ve disk önceliğinde 6,28 saniyede
tamamlandı. Dosyalar yalnız okunup hash'lendi; kod import edilmedi, model
yüklenmedi, cache kopyalanmadı, çalışan AOS değiştirilmedi. Bu doğrulama
manifest ile yerel byte eşliğini kanıtlar; gerçek inference, GPU belleği,
adil devir veya eşzamanlı ilerleme kabulü hâlâ açıktır. İzole entegrasyon
başlangıcında bu kimliklerin değişmediği yeniden kontrol edilmelidir.

## 2026-09-25 — 0.18 deneyinde yeniden dış GPU tüketicisi

Ortak kuyruğa katılmayan Python süreci (`session-19.scope`, 3894 MiB) ikinci
gerçek araştırma denemesini baseline aşamasında durdurdu. Kaynak uygulama
AOS olarak doğrulanmadı; dış süreç değiştirilmedi. `local-qwen-018-wire-review.json`
ve `local-qwen-018-wire-outcome.json` kanıtı korur. Lab model çağrısı başlamamıştı.

Paralel AOS çalışması için gereken entegrasyon aynı: Decider/Bonsai/vision
CUDA yüklemeleri ortak yöneticiden geçmeli; ayrı test oturumunda adil devir
ölçülmeli. Dış GPU süreçleri bu kuyruğa katılmadan iki uygulamanın birbirini
engellemeyeceği garantisi verilemez. Canlı AOS mevcut dosyaları ve süreçleri
değişmedi; dar yama izole `source-gpu` kopyasında geliştiriliyor.

## 2026-09-25 — güncel public AOS kaynağına izole rebase

Güncel canlı AOS'un public kaynak görüntüsü ile typed Lab/GPU değişiklikleri
ayrı `data/runtime/aos-coexistence/rebase-f16d3ced7e6d430eb9b2e11913dac720/merged`
kopyasında birleştirildi. Canlı dosyalar, süreçler, modeller ve DB değişmedi.
`source-current`, `source-typed` ve eski `source-gpu` korunuyor.

Üç çakışma çözüldü: `reusable_decider.py` CPU prewarm 600 saniye, worker
yeniden hazırlama ve EOF/bozuk JSON hata yollarını; `serve_desktop.py` idle
argümanı, form seçenekleri ve form metadata kapsamını; `desktop_console.py`
yeni form/restart-quiesce kodunu korur. Typed Lab araçları/ayrı iş kayıtları
ve opt-in GPU hook'ları da korunmuştur. Kaynak manifestinin gerisinde kalan
altı yeni public dosya ayrıca hash'lenip kopyalandı; özel runtime/veri yok.

**55 hedefli test / exit 0**, **4096 paket kontrolü / exit 0**. Gerçek
`serve_desktop.py --help` importu ve yeni/eski seçeneklerin görünmesi exit 0.
İlk yanlış interpreter yolu ve eksik public fixture gate hatası kanıtta
korunur; dependency kurulumu veya canlı source düzeltmesi yapılmadı.

Teslim `review-evidence/aos-gpu-rebased-source.patch`; kaynak/komut/sonuç
bağı `review-evidence/aos-gpu-rebase-review.json`. Yama captured live kaynak
görüntüsünde uygulanıp **20/20 değişen dosyada byte eşliği** verdi. Canlı
MANIFEST yamaya dahil edilmez; güncel kaynakla uzlaştırılınca yeniden üretilir.
Bu yama doğrudan canlı AOS'a uygulanmış veya gerçek GPU kabulü almış değildir.

Yeni `/api/restart/quiesce` yolunun detached Lab işleriyle stop/kurtarma bağı,
normal `serve_desktop` içinde typed Lab coordinator bootstrap'ı ve gerçek
Decider/Bonsai broker çalıştırıcısı açık kalır. GPU testinde kullanılacak
sonraki AOS kodu bu rebased kopyada devam edecektir.


## 2026-09-25 — gerçek broker kanıtı ve dış GPU tüketicisi

Son izole broker yaması 24 dosyadır; 57 hedefli test/4100 paket kontrolü
exit 0, patch uygulaması byte eşliğiyle doğrulandı. Kaynak ve sınırlar
`20-aos-broker-model-review.md` ve `aos-lab-gpu-broker-patch-review.json`
içindedir. Dört gerçek tanı çağrısı Qwen S1 → Decider → Qwen S2 → Bonsai
sırasında tamamlandı (session 52269 / exit 0); tam araştırma/desktop kabulü
henüz değildir. Canlı AOS'a kurulum yapılmadı.

Son tek S2 denemesinde ortak kuyruğa katılmayan dış Python GPU tüketicisi
model sonucu oluşmadan testi durdurdu (session 44215 / exit 1). Kaynağın
AOS olduğu doğrulanmadı; dış sürece müdahale edilmedi. Kanıt
`local-qwen-022-s2-syntax-diagnostic.json`. Paralel AOS çalışmasının GPU
çağrıları da aynı opt-in broker protokolüne katılmalıdır. Bu koordinasyon
olmadan bağımsız GPU denemelerini kör tekrarlamak birlikte çalışma kabulü
sağlamaz. Lab CPU/public veri/holdout işi devam eder; canlı AOS kaynakları
ve model ayarları paralel geliştirmeyle uzlaştırılmadan değiştirilmez.


## 2026-09-25 — normal başlatma ve model bekleme yarışı

Normal `serve_desktop.py` opt-in GPU broker'ını kuruyor ancak Lab dış iş
koordinatörünü henüz console'a bağlamıyor. `start()` model kararını
beklerken HUMAN lease değişirse eski yetkiyle Lab çağrısı yapılabildiği
izole SQLite/fixture testinde yeniden üretildi (exit 1); canlı AOS veya
gerçek model kullanılmadı. `23-provider-syntax-and-aos-lifecycle-review.md`
kanıtı ve sınırı açıklar. Luna yeni public test kopyasında `lab_external.py`,
`serve_desktop.py`, `desktop_console.py`, ilişkili test/docs/manifest üzerinde
çalışıyor. Karar sonrasında yetki kontrolü, normal bootstrap, restart/quiesce
ve en fazla sekiz aktif dış iş kapsam içindedir. Canlı AOS'a yazılmıyor;
paralel oturumun aynı dosyalardaki değişiklikleri aktarım öncesi uzlaştırılacak.

## 2026-09-25 — ortak veritabanı ve ayrı 0.27 çalışma kopyası

Lab 0.27, broker'ın sabit
`~/.local/state/swapp-gpu/arbiter.sqlite3` dosyasını kullanabilir. Broker,
her araştırma koşusunun canonical Director unit kimliğini de doğrular;
PID başlangıcı, boot, InvocationID ve cgroup kontrolleri korunur.
Kaynak commit'i `390a6c25e94e5da339457dd17e88749ed07c7795` ve kalite
bağı `review-evidence/parallel-integration-027-quality-gate-binding.json`.

`data/runtime/parallel-m0/public-execution-027` ayrı çalışma kopyasıdır.
Public v3 manifesti, research profili, private API principal ve rol bazlı
DB bağlantıları hazırlandı. Üretim kayıt defteriyle manifest/config
hash'leri ve modül kökleri doğrulandı; gerçek preflight exit kodu 0.
Model dosyaları ayrı salt okunur reflink kopyalarıdır; model hash kontrolü
çalıştırma öncesinde yine zorunludur. Hazırlık hiçbir API, broker veya
GPU modeli başlatmadı. Kanıt:
`review-evidence/private-public-execution-027-preflight-outcome.json`.

0.25 S2 denemesinde bağımsız GPU'ya giren süreç bu kez AOS'un
`llama-server` dosyasına ve `session-28.scope` cgroup'una bağlandı
(`review-evidence/local-qwen-provider-025-outcome.json`). Yalnız Lab'ın
kendi model süreçleri durduruldu. Gerçek birlikte çalışma denemesinden
önce paralel AOS oturumunun GPU çağrılarını ortak broker'a geçirmek veya
koordineli bir test aralığı ayırmak hâlâ gerekiyor. Kullanıcıya iletilen
koordinasyon sorusunun yanıtı bekleniyor; CPU geliştirmesi sürüyor.

Luna kolları holdout, eğitim ve AOS deneme sürücüsü üzerinde ayrı
kopyalarda çalışıyor. İzole AOS yamaları henüz canlı AOS'a uygulanmadı.
Bu hazırlık gerçek araştırma, eğitim veya birlikte çalışma kabulü değildir.


## 2026-09-27 — V4 sürücüsünün CPU düzeltmeleri

İzole V4 deneme sürücüsünde başlatıcı/readiness zaman aşımı sonrası exact
generation yakalama, kendi süreçlerini temizleme ve drain hatasının başarıyı
geçersiz kılması düzeltildi. İki sistemin ilerlemesi artık aynı aktif aralıktaki
provider ilerlemesi ve broker ticket geçişleriyle denetlenir. Dondurulmuş kaynakta
11 CPU sözleşme testi ve Ruff exit 0; kanıt
`review-evidence/aos-lab-coexistence-v4-cpu-review.json`. Gerçek AOS/GPU/model
çalıştırılmadı ve canlı AOS kaynakları değiştirilmedi. HUMAN typed Lab start,
M0.AOS.5 için gerçek modelin başlatma kararı sayılmaz.

GPU/AOS koordinasyon yanıtı hâlâ bekleniyor. Holdout bütçe/recovery düzeltmeleri
ve Farm B kaynak incelemesi bağımsız CPU kollarında sürüyor;
`32-holdout-integration-review.md` kapsamı ve eksikleri kaydeder.

## 2026-09-30 — izole gerçek CPU restart/stop ve dar AOS yaması

`review-evidence/aos042-wire-stop-recovery.patch`, dondurulmuş birleşik AOS
kaynağına iki ek taşır: Lab status yanıtındaki typed `purpose` alanı ve yeni
açık HUMAN binding için session/generation ile bağlı stop idempotency anahtarı.
Önceki süreçten kalan `uncertain` intent değiştirilmez; aynı güncel binding
altındaki stop tekrarı aynı action'ı kullanır. Yama canlı `/home/cachyos/aos`
kaynağına uygulanmadı. Paralel AOS çalışmasıyla bu dar dosya değişikliği
koordine edilerek ayrıca uygulanmalı ve doğrulanmalıdır.

Gerçek ayrı AOS süreçleri, SQLite reopen, Lab HTTP, lease yarışı/eşzamanlı
start ve TCP console outage/rebind yolları izole CPU koşusunda ölçüldü.
R2'nin 21 başarılı kontrolü sonrasındaki barrier timeout sonucu **exit 1**
olarak korunur; R3 ayrı devam koşusu aynı Lab run üzerinde yeni HUMAN lease,
explicit recover, iki idempotent stop ve foreground için **10/10, exit 0**
verdi. İlk stop-intent recovery hatası da ayrı başarısız kanıttır.

Normal Director stop öncesinde aktif, sonrasında aynı sahipli unit inactive
ve MainPID=0 gözlendi; dispatcher dış bekleyicisinin **exit 1** sonucu başarı
olarak sunulmaz. Gerçek Scorer claimed/inflight aralığı yakalanmadı. Run
ilk stop sonrasında `stop_requested` durumundaydı; ilk gözlem terminal
raporu kanıtlamıyordu. Gerçek Scorer inflight drain, model/GPU/public veri
ve tam M0.AOS.2/.3 kabulü açık kalır. Fixture desktop ve karar motoru
açıkça etiketlidir.
Kaynak ve dar icra özeti:
`review-evidence/aos042-cpu-lifecycle-summary.json`.

R4 teslim kopyası, ölçülmüş R3 kaynaklarını değiştirmeden yalnız ilgili
MANIFEST hash satırını güncelledi. Tam ek yama
`review-evidence/aos042-wire-stop-package-r4.patch` içindedir. Ayrı temiz
test kopyasında check/apply exit 0 ve 1095 kaynak üyede byte eşliği;
required package validation **4360 kontrol, session 36491 / exit 0**.
`review-evidence/aos042-package-r4-summary.json` icra bağını taşır.
[Bağımsız ve AOS opt-in başlatma](63-aos-opt-in-start.md) iki yolu ve özel
yapılandırma şartlarını belgeler; normal GPU/Decider bootstrap bu teslimatta
çalıştırılmadı ve canlı AOS'a yama uygulanmadı.

Aynı run için normal `director recovery inspect` exit 0, ardından
`director recovery apply` session 92579 / terminal `19632a` / exit 0:
`finalized_stopped`, bağımsız finalizer exit 0 ve recovery completed.
Ayrı gerçek API readback `8f1b36` / exit 0, HTTP 200 ile `stopped`
durumunu ve rapor hash
`18f12a5d2d5efa712eb324d06f7bfe38d711152fd351f9724198b5f760ae0825`
eşliğini doğruladı. Önceki dispatcher exit 1 korunur; otomatik tek stop
isteğiyle terminal kapanış ve Scorer inflight drain hâlâ kanıtlanmadı.
