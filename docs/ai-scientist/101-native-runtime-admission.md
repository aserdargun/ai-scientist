# Gerçek runtime admission bağlantısı

2026-10-01. Scientist tabanı `985348b41d5a30f677556875278666ede288fbf1`;
AOS referansı `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` ve yerel kaynakları.

## Tamamlanan kod

- `aos_native_artifact_receipts.py`: eski model kontrol çıktısı yeterli değildir.
  Yeni native doğrulama, artifact/interpreter/source kimliklerinin önce/sonra
  snapshot'larıyla bağlıdır. Kullanımda bağımsız receipt hash'i, boot/BOOTTIME
  geçerliliği, tam profile/binding eşleşmesi, dosya kümesi/kimliği, kaynak byte'ları,
  gerçek model Python'unda tam dependency VERSION kümesi ve current rights denetlenir.
  Model ağırlıkları her çağrıda yeniden hash'lenmez; native worker'ın activation
  öncesindeki original hash kapısı korunur. Kimlik sürekliliği sürekli byte
  gözetimi veya tam Python package byte attestation olarak sunulmaz.
- `aos_native_live_bindings.py`: üç tam binding'i ACK kullanmadan actual
  canonical broker ve AOS systemd MainPID/start/boot/invocation/cgroup kimliğinden
  üretir. Config/policy hash'leri, enabled policy, source/profile pinleri ve
  generation tekrar kontrol edilir. İstenirse canlı Lab principal'i de doğrular.
  Scheduler/DB/servis yaratmaz; ikinci tahsis otoritesi değildir.
- `check_aos_model_environment.py`: independently hash-bound closure input ile
  yeni native verification receipt üreten komut yolu eklendi. Çıktı yalnız yeni
  Scientist-owned runtime dosyasına yazılır; ham tam receipt konsola basılmaz.
  Config source closure'ı yeni üretici/sağlayıcı/helper dosyalarını da pinler.

Yeni CLI yolunun şekli:

```text
<mevcut Decider model Python'u> -B scripts/check_aos_model_environment.py
  --receipt <bağımsız worker/manifest review receipt'i>
  --expected-receipt-sha256 <review receipt hash'i>
  --closure-input <bağımsız canlı bindings/requirements/source input dosyası>
  --expected-closure-input-sha256 <input hash'i>
  --closure-output <yeni Scientist runtime receipt dosyası>
```

Bu komut şekli launch planıdır. Canlı tam binding'ler ve gerçek current-rights
sağlayıcısı bu koşuda provision edilmedi; yeni CLI/receipt üretim yolu gerçek
runtime admission ile çalıştırılmış sayılmaz. Önceki gerçek model artifact
kontrolü [100 numaralı teslimde](100-configured-source-model-readback.md) kayıtlıdır.

## Somut kalan sınır

Native model kararının mevcut production composition'ı desktop scheduler'dır.
`ScientistDesktopBinding._current` gerçek desktop task, current persisted State,
lease ve matching active runtime ister. `ScientistLabService` typed Lab kontrolünü
sağlar, fakat Decider seçiminden Lab işine bağlanan native headless runtime yoktur.

Actual `DesktopRuntime.start` workspace'i **AOS `data/` altında** zorunlu tutar.
Kullanıcının son talebi AOS'u salt okunur tutmaktır. Bu nedenle AOS data dizini
açılmadı. Sadece bu koşuya ait yeni geçici runtime dizini için kapsam sorusu
bekliyor; yanıt varsayılmıyor. Alternatif, AOS oturumunun Scientist-owned workspace
destekli native runtime sağlamasıdır. Dummy image pin/session satırı veya sentetik
runtime kimliği bu eksik yetkinin yerine konulamaz.

Actual `models/desktop-manifest.json`, `computer/` kaynak dosyaları ve mevcut
Docker image ID/source label salt okunur kontrol edildi: source set/digest ve
image label eşleşti, `docker image inspect` exit0. Container başlamadı; AOS'a
yazılmadı. Mevcut native yol için yeni image/model indirmesi gerekmiyor. Bu
preflight desktop çalışmasının veya GPU koşusunun gerçekleştiğini kanıtlamaz.

Canonical broker yerleri `/run/user/<uid>/swapp-gpu` ve
`~/.local/state/swapp-gpu/arbiter.sqlite3` kalır. Özel fixture DB'ye taşımak ortak
tahsis kabulü değildir. Son denetimde canonical broker yok/inactive/MainPID0;
bu gözlem gelecekteki koşu için rezervasyon değildir.

## Geçen / kalan / çalıştırılmayan

- **Geçen:** 54 fixture kontrolü, exit0/0,395619243 saniye. Native generation
  binding parity, yanlış/eskimiş kimlik, revoked policy/principal, timeout,
  expired/drifted artifact receipt ve izinli runtime alias'ları kapsandı.
  İlk denemede 6 fixture hatası/48 geçen vardı: registry lookup eksik fixture
  manifest'ine gidiyordu. Test lookup'u açıkça stublandı; production manifest
  doğrulaması gevşetilmedi. Bu testler actual service/GPU kanıtı değildir.
- **Kalan:** AOS workspace kapsamı; actual live bootstrap/receipt/current rights
  composition; tek canonical scheduler rezervasyonu ve kullanıcı işi çakışma
  denetimi; source pinleri sabitken bounded GPU kabul koşusu.
- **Çalıştırılmayan:** yeni actual service binding capture, canlı configured-source
  admission/receipt, AOS gerçek görev → GPU devri → Scientist gerçek öneri/deney
  → bağımsız Scorer → AOS sonuç doğrulaması; allocated iptal/toparlanma, birlikte
  yük/fairness, öğrenilmiş adaptör. VRAM ve GPU gecikme ölçümü yok.

Kalite kapısı **2057 passed / 7 opt-in skipped / 120 GPU-live deselected**;
yedi komut exit0, parent204,214428767 saniye, Python kaynakları sabit.
Kabul **11 geçti / 7 kısmi / 4 açık**. AOS kaynakları/data'sı ve diğer Codex
dosyaları/süreçleri değiştirilmedi; push/merge/deploy yok. Lisans ve genel CI ayrı.

## AOS oturumuna aktarılacak kısa beklenti

Scientist native60/source verifier + native factory/provider ve output-v2
sözleşmesini kullanıyor. Gerçek model Python'u uygulama Python'undan ayrıdır.
Canlı kimlik capture ve native artifact receipt sağlayıcıları hazırdır; ACK'ten
expected hak türetilmez. Mevcut native model task AOS-data desktop runtime istiyor.
Scientist-owned kilitli workspace üzerinde gerçek native runtime/task binding
sağlayın veya açıkça yetkilendirilmiş geçici AOS-data yolunu kullanabilelim.
Session/lease/generation, actual systemd identity/clock ve approval semantiği
korunmalı; dummy desktop kimliği eklenmemeli. GPU kabulünün tek yürütücüsü
Scientist olacak. Koşu sırasında seçili source/policy pinleri değişirse admission
reddedilir; stop/idle/EOF release kanıtı değildir.

## Güncel yürütme yolu — 2026-10-01

Actual checkout tekrar incelendi: `src/aos/desktop.py:50–53` dış workspace'i
reddediyor. Bu sınırı keşfetmek için `start()` çağrılmadı; çağrı önce Docker
incelemesi yaptığı için sınır kaynak üzerinden doğrulandı. AOS'a yazılmadı.

Scientist-only araştırma bu engelden bağımsızdır. Mevcut `lab director
dispatch-one` gerçek run-bound Lab servisinden `LocalQwenProposalProvider` →
`OwnedVllmRuntime` → aynı canonical `SharedGpuScheduler` DB'sine gider. Boş AOS
unit eşlemesinin geçerli adı gerekir; çalışan AOS görevi veya broker daemon'u
Lab tahsisi için zorunlu değildir. Bu yol AOS birlikte çalışma kabulü değildir.
Yeni bir launcher/tahsis otoritesi gerekmiyor. Başlatmadan önce mevcut
`SystemdUnitManager.ensure_slice()` ortak slice'ın toplam limitlerini hazırlamalı
veya doğrulamalı; aksi durumda `systemd-run --slice` limitsiz slice yaratabilir.
Director dispatch/resume başlatıcısı bu kontrolü servis açılmadan önce yapacak
şekilde düzeltildi; geçersiz UUID/bütçe/environment için servis yan etkisi yoktur.

Canlı `swapp-gpu.slice` active/loaded; `MemoryMax`, `MemorySwapMax`,
`CPUQuotaPerSecUSec`, `TasksMax` değerlerinin **tamamı infinity**. İncelenen
slice ve alt cgroup `cgroup.procs` listeleri boş, `memory.current=25313280`.
Boş süreç listesi slice sahipliği izni değildir; mevcut ortak slice'ın limitleri
değiştirilmedi. Actual native verifier uyumsuz sınırları reddeder. Gerçek koşu
öncesinde ortak slice sahibiyle bounded bütçe kurulumu koordine edilmelidir;
başka slice/DB oluşturarak ortak tahsis sınırı aşılmayacaktır.

Ana ledger yalnız `READ ONLY` transaction/3 saniye statement timeout ile
incelendi: 4 completed, 1 failed, 1 running, 2 stopped, 1 stop_requested.
Console'daki 5 watched run terminaldir; watched liste tüm ledger değildir.
`director recovery inspect` iki nonterminal local-qwen koşu için `owner=null`,
`owner_proven_dead=null`, aktif score-job listesi boş döndürdü. Running koşunun
5 experiment'i terminal; stop_requested koşuda 1 eksik experiment/trajectory
çifti var. Sahiplik kanıtı olmadan bu koşular kapatılmadı, tekrar başlatılmadı
ve kaynakları serbest bırakılmadı. Yeni kabul koşusu bunları benimsemeyecek.

Bu gözlemler GPU rezervasyonu veya yeni araştırma başarısı değildir. Gerçek
öneri/deney/bağımsız Scorer/rapor ve AOS devri hâlâ çalıştırılmalıdır.

İzolasyon incelemesinde ek gerçek eksik bulundu: Director/Planner DSN path'leri
ayrı yapılandırılabiliyor, fakat Scorer supervisor'ın yeni worker/finalizer
komutu `LAB_SCORER_DSN_FILE` seçimini taşımıyor. Worker copy-relative default
Scorer DSN'ine döner. Yalnız Director/Planner DB'sini değiştirerek izole kabul
koşusu başlatmak güvenli değildir; yanlış ledger'a gider. Sonraki uygulama
adımı trusted Scorer yapılandırmasının worker/finalizer'a tutarlı taşınmasıdır;
Scorer DSN içeriği Director/API'ye okunmayacak veya sonuçlarda yayımlanmayacaktır.

Başlangıç düzeltmesinin odaklı kontrolü **58 passed / 1 opt-in skipped**, exit0;
mandatory gate **2066 passed / 7 opt-in skipped / 120 GPU-live deselected**,
yedi komut exit0, 205,079269296 saniye, Python kaynakları sabit. Bunlar startup
ve regresyon kanıtlarıdır; yeni model/araştırma/AOS kabulü değildir.

[Kaynak/fark hash'leri, gate ve salt okunur image kontrolü](review-evidence/native-runtime-admission.json).
