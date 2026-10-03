# Özgün worker cleanup kaydının bağımsız fiziksel doğrulanması

2026-10-01; Scientist tabanı `0cc80c8045b79571457e3ded62784d24b5b4f67b`,
salt okunur gözlenen AOS HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
AOS paralel değişiyor; bu çift birlikte dondurulmuş bir deployment değildir.

## Tamamlanan uygulama

`ControlStore.read_original_physical_snapshot()` mevcut retained-target yetkisiyle
tek query-only SQLite transaction'ında özgün intent, ticket, terminal, allocation,
drain, child ve arbiter kayıtlarını doğrular. Owner/generation/fence, özgün bütçe,
full-frame ve ayrı canonical payload hash'i korunur. Rehash edilmiş tutarsız drain
kanıtı, aktif/quarantined özgün tahsis ve eksik tablolar reddedilir. Okuma kontrol
ID'si tüketmez, scheduler kurmaz ve DB'ye yazmaz.

`LabAOSControl.verify_original_physical_cleanup()` bağımsız kaynak okumasını
salt okunur systemd/cgroup/proc/GPU gözlemleriyle birleştirir. Özgün invocation,
nonce, boot, PID/start ticks ve kaynak limitleri doğrulanır; original cgroup ve
PID'lerin yokluğu iki kez kontrol edilir. Öncesi/sonrası kaynak ve current
authority tekrar doğrulanır. Başka lane'in GPU süreçleri çalışabilir; eski PID'nin
yeniden kullanılması konservatif reddedilir. Belirsiz cleanup tahsis bırakımı
üretmez. Bu fonksiyon stop/release çağırmaz ve AOS journal çözümleme yetkisi vermez.

Fiziksel facade opt-in'dir: private policy'nin Scientist source map'i ayrıca
`lab/llm/aos_physical_readback.py` hash'ini pinlemelidir. Eksik pin, source drift,
bootstrap infer capability ve revoke kabul edilmez. Wire evidence-v3 ve iç
evidence-v2 sözleşmeleri değiştirilmedi.

## Kanıtın sınırı

77 odaklı CPU kontrolü parent exit0 ve sabit Python kaynaklarıyla geçti. SQL
orijinal okuyucusu gerçek koddur; authority/model/unit/GPU gözlemleri fixture'dır.
İlk iki odaklı koşu test fixture cgroup uyuşmazlığından başarısız oldu; gerçek
deployment slice'ına uyarlandıktan sonra 74, facade kontrolleriyle 77 geçti.
Bu sonuç gerçek GPU cleanup veya birlikte çalışma kabulü değildir.

Model/backend/quantization/context seçilmedi; VRAM tepe, bekleme/devir/model
gecikmeleri ölçülmedi. CPU süreleri model gecikmesi olarak sunulmaz.

## AOS oturumuna aktarım

Mevcut authenticated parent, gerçek current PeerGeneration ve başarılı retained
discovery capability SHA ile bu facade'a bağlanmalı. AOS callback girdileri özgün
target/profile/deployment'a adapte edilmeli; candidate witness bağımsız okuma
yerine kullanılmamalı. Fiziksel readback journal resolution yetkisini tek başına
sağlamaz. AOS observer'ının varsayılan quiet-compute modu foreign CUDA
context'lerini reddeder; güncel kaynak ayrıca `allow_shared_lanes=True` sunar.
Scientist burada yalnız özgün worker'ın yokluğunu kontrol eder. Shared-lane
ayarının source/current/device sözleşmesi birlikte doğrulanmalı; quiet GPU devri
ve shared-lane doğrulaması aynı kabul gibi gösterilmemeli.

Salt okunur paralel incelemede authenticated cross-interpreter provider henüz
bulunmadı. Sonraki bağlantı aynı canonical broker nesnesini kullanmalı; ayrı
ControlStore kopyası yetki değildir. AOS physical callback'inin eksik
`result_canonical` alanı exact committed reconcile ACK closure'ından alınmalı;
broker bu aday evidence'ı bağımsız orijinal kaynakla karşılaştırmalıdır.
Inherited-channel seçilirse FD bootstrap ve gerçek AOS service generation'ı
açıkça bağlanmalı: sıradan inherited socketpair `SO_PEERCRED` oluşturucuyu
gösterir, mesaj gönderenini kanıtlamaz. Bu bağlantı henüz uygulanmadı.

## Kalan ve çalıştırılmayan kabul

Authenticated ayrı AOS interpreter sağlayıcısı, final source/config closure,
canonical broker rezervasyonu, gerçek bounded model → Scientist deney → bağımsız
Scorer → AOS rapor doğrulama, gerçek iptal/toparlanma ve adil iki taraf ilerleme
koşusu açık. Gözlem sırasında canonical service load `not-found`, SQLite dosyası
yoktu; servis kurulmadı, ikinci allocator oluşturulmadı. Canlı AOS/süreç/cache
değiştirilmedi. Push/merge/deploy yapılmadı.

M0 kabul toplamı **11 geçti / 7 kısmi / 4 açık** olarak korunur. Lisans ve genel
CI işleri ayrı kalır. [Hash ve icra kaydı](review-evidence/original-physical-cleanup-readback.json).

Son zorunlu kalite kapısı **1865 passed / 7 opt-in skipped / 120 GPU-live
deselected**, yedi komut exit0; parent194,064680620 saniye ve Python kaynakları
sabit. İlk kapıda testler geçti, mypy optional policy erişimini reddetti;
None kontrolü eklendikten sonra final kapının tamamı geçti.
