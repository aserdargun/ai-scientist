# Scorer deployment veritabanı seçimi

2026-10-01. Scientist tabanı `5683ab62fd5b760cbed8cd7a81cfd5fefab74449`;
AOS salt okunur referansı `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.

## Tamamlanan uygulama

`LAB_SCORER_DSN_FILE` artık operator deployment yapılandırmasından bütün Scorer
alt süreçlerine taşınır. API istekleri credential path seçemez. Director yalnız
dosya yolunu taşır; credential içeriğini okumaz. Mevcut Scorer/CARE yetkili
işlemleri credential içeriğini kendileri okur.

Score/recovery, paired/empty finalizer, mode installer, holdout ve recovery
çeşitleri, CARE cell ve CARE CLI aynı seçiciyi kullanır. Director dispatch/resume
allowlist'i de yolu taşır. Değişken verilmezse eski default korunur. Açıkça
geçersiz bir deployment seçimi default veritabanına düşmez; servis/slice işlemi
öncesi hata verir.

Dosya absolute, current UID-owned, mode0600, regular ve tek bağlantılı olmalıdır;
symlink, güvensiz ancestor, boş/aşırı path veya dosya reddedilir. Kontrol metadata
üzerindedir; parent credential byte'larını okumaz. Root/current UID-owned
ancestor'lar ve root-owned sticky `/tmp` desteklenir. Normal güvenli0755 dizinler
reddedilmez.

Operator yeni deployment için Director/Planner ve Scorer DSN dosyalarını aynı
ledger'a bağlar; Scorer yolunu API/Director servis ortamına koyar. Token veya
DSN içeriği argv, rapor veya public belgeye yazılmaz. Model/source kopyası ve
yeni GPU tahsis otoritesi gerekmiyor.

## Gerçek CPU kanıtı

Mevcut immutable Postgres16-bookworm image'i kullanıldı:
`sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67`.
Yeni, yalnız bu koşuya ait container512MiB/no-swap/0,5CPU/pids128;
loopback port32779, dört ayrı DB rolü, schema0040. Yeni model/image indirilmedi.

Production `run_mode_snapshot_install` gerçek systemd Scorer worker'ını
başlattı; worker seçilen **yeni** Postgres'e bir sentetik profile kaydı yazdı.
İzole profile sayısı0→1; `current_user=swapp_lab_scorer`, bağımsız postmaster
başlangıcı `2026-10-01 03:13:04.142337+00:00`. Native çağrı1,088291532 saniye;
worker `installed` döndürdü. Scorer aggregate slice2GiB/no-swap/100%CPU/pids32.

Ana ledger yalnız read-only/3 saniye statement timeout ile önce/sonra incelendi:
17 profile, 4 completed/1 failed/1 running/2 stopped/1 stop_requested run değişmedi.
İzole profile ana DB'de yok. Owner kanıtı eksik iki eski run'a dokunulmadı.

Worker unit terminal: inactive/MainPID0, collected/not-found. Yalnız bu koşunun
exact ID/label/image'i doğrulanmış container'ı durduruldu; exited/PID0 sonrası
silindi, Docker'da yokluğu doğrulandı. İzole DB'ye ait `installed.json`/`suite.json`
marker'ları yeni sentetik snapshot'tan kaldırıldı; kapanmış DB için ana deployment
readiness iddiası bırakılmadı. Yeni kaynak **uninstalled** olarak tutuldu.

Bu kanıt gerçek alt süreç/credential routing kanıtıdır. Bağımsız model puanlaması,
yerel LLM araştırması, GPU devri, AOS birlikte çalışma veya eğitim kanıtı değildir.
Model/quantization/context, GPU VRAM tepesi ve GPU gecikmeleri bu koşuda ölçülmedi.

## Kontroller ve kalan işler

134 odaklı kontrol geçti; 1 isolated PostgreSQL opt-in kontrolü skip. İlk komutta
olmayan bir test filename'i kullanıldı (exit4, hiçbir test çalışmadı); doğru
dosya listesiyle exit0. Cleanup gözlemindeki ilk sistem Python'u SQLAlchemy
içermiyordu; hiçbir cleanup işlemine ulaşmadı. Aynı canlı container mevcut venv
ile gözlendi ve yukarıdaki cleanup doğrulandı.

Zorunlu kalite kapısı **2121 passed / 7 opt-in skipped / 120 GPU-live deselected**;
yedi komut exit0, 206,818752023 saniye, kaynaklar sabit. M0 sayıları
11 geçti / 7 kısmi / 4 açık olarak kalır. Yeni gerçek local-Qwen
öneri→deney→bağımsız Scorer→rapor koşusu ve AOS birlikte çalışma kabulü açık.
Ortak GPU slice bütçesi aşağıdaki yetkili runtime hazırlığıyla kuruldu;
AOS runtime workspace sınırı ayrıca ele alınmalıdır.
Lisans ve genel CI işleri ayrı tutulur; push/merge/deploy yapılmadı.

## AOS oturumuna aktarılacak kısa durum

Scientist Scorer deployment izolasyonu source/model kopyalamadan çalışıyor.
Canonical scheduler/DB, native fencing, output-v2/provider sözleşmesi korunuyor.
Native AOS broker worker dosyaları actual checkout'ta mevcut. GPU kabulünün tek
yürütücüsü Scientist; ortak GPU slice aşağıdaki canonical limitlerle hazırlandı.
AOS dış workspace yeteneği üzerinde koordinasyon gerekiyor. Stop/idle/EOF hâlâ
GPU release kanıtı değildir.

## Ortak GPU runtime hazırlığı

Salt okunur ek inceleme, `swapp-gpu.slice` tanımının source/fragment/drop-in
dosyası olmayan implicit slice olduğunu gösterdi; creator/session atfedilemedi.
İki gözlemde aynı InvocationID ve cgroup inode18665, populated0, boş process
listesi ve alt cgroup yoktu. Canlı API/drain/console/tunnel app.slice içindeydi.
Bu, kullanıcının yetkilendirdiği bounded test runtime hazırlığı kapsamında
ek izin beklemeden ilerlemek için somut no-collision kanıtı sağladı.

Scientist'in **mevcut** Director global dispatch lock'u ve koordinasyon lock'u
alındı. Slice kimliği/boşluğu ve GPU/dispatch/resume servislerinin yokluğu tekrar
doğrulandı. Yalnız runtime property'leri16GiB RAM/zero swap/200%CPU/pids128 olarak
kuruldu; source dosyası, servis veya başka oturum süreci değiştirilmedi.
Native `SystemdUnitManager.ensure_slice()` ve actual cgroup dosyaları doğruladı:
`memory.max=17179869184`, `memory.swap.max=0`, `cpu.max=200000 100000`,
`pids.max=128`. Aynı InvocationID/inode ve empty subtree korundu; lock'lar bırakıldı.
Production `ensure_slice()` uyumsuz limitleri otomatik değiştirmez.

Bu runtime hazırlığı GPU tahsisi değildir: canonical scheduler rezervasyonu,
model çağrısı, VRAM tepesi ve birlikte çalışma kabulü henüz ölçülmedi. AOS
workspace sorusu beklerken Scientist-only gerçek araştırma hazırlanabilir.
