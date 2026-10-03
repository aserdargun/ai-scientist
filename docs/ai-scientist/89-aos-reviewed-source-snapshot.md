# Gerçek AOS checkout'u için review edilmiş snapshot

2026-10-01. Scientist tabanı `54f74cafc2e18312751a825d9dbe948575c94015`;
AOS HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.

## Tamamlanan değişiklik

`check_aos_lab_compatibility.py` artık açık `reviewed_snapshot` modunu destekler.
Temiz/commit edilmiş AOS worktree kullanıcı şartı değildi; bu mod paralel
çalışılan gerçek checkout'u review edilmiş **HEAD + tracked binary diff + seçili
kaynak manifesti + seçili untracked manifesti** ile tanır. Dört beklenen pin
zorunludur. Eski varsayılan modun dirty/untracked ret davranışı korunur.

Client39 ve journal44 profilleri gerçek AOS observer'ındaki açık üyeliklerle
ayrı ayrı doğrulanır. Journal profili migration0024, storage ve dataset audit
bağımlılıklarını içerir. Kaynaklar AST/byte olarak okunur; AOS import edilmez,
SQL/test/model çalıştırılmaz. HEAD, status, diff, tracking ve kaynaklar iki kez
okunur; gözlenen değişim ret sebebidir. Bu seçili kaynak incelemesidir, bütün
runtime/bağımlılık/config closure için attestation değildir.

Salt okunur inceleme Git'in harici program çalıştırmasına izin vermez. Diff
`--no-ext-diff --no-textconv --binary HEAD` kullanır. Effective Git clean/process
filter yapılandırması varsa status/diff öncesinde
`git_content_filter_configured` ile reddedilir; Git config değiştirilmez.
External diff, textconv ve clean filter için üç marker regresyonu önce gerçekten
başarısız oldu; düzeltmeden sonra 58 odaklı CPU kontrolü geçti. Kanıtlar
[makine kaydında](review-evidence/aos-reviewed-snapshot-source-delivery.json).

Zorunlu kalite kapısının yedi komutu exit0: **1745 passed / 7 opt-in skipped /
120 GPU-live deselected**. Parent 140.355737438 saniye; Python kaynakları koşu
boyunca ve teslim kontrolünde aynı hash’te.

## Gerçek checkout gözlemi

Journal44 seçili kaynak SHA:
`60a1acbd25fd0c9c71da7e4b257b6f075e83ed6d96649b485ca817cfaed2dfd7`

Tracked binary diff SHA:
`205f41ae277fdb960566f830bb576de048c692facb4d728d1ea0155fbd63fc1f`

Seçili untracked manifest SHA:
`8f5a2ed0e263ff072e8ec3394b17938f043edb260914839a996118e70d06f212`

Bu sabit beklenen pinlerle gerçek CLI **exit3 / pending / source_ready=true**
döndürdü. `admission_allowed=false`; tek kalan kaynak-rapor maddesi
`joint_runtime_capability_confirmation_pending`. Exit3 burada kaynak uyumu
sonrası runtime teyidinin beklediğini gösterir. Varsayılan eski mod aynı
checkout'u dirty/untracked nedeniyle unsupported olarak reddetti.

Bu pinler tarihli gözlemdir. Yeni değişiklik varsa otomatik güncellenmez;
yeni kaynak review'ü gerekir. AOS checkout'u ve süreçleri değiştirilmedi.

## Gerçek koşu için kalan somut işler

Salt okunur host envanterinde `swapp-lab-gpu-broker.service` LoadState=not-found,
ActiveState=inactive; beklenen özel broker env dosyası ve canonical
`~/.local/state/swapp-gpu/arbiter.sqlite3` bulunmadı. Bu yollara yazılmadı, servis
kurulmadı veya başlatılmadı. Scheduler rezervasyonu doğrulanmış değildir.
Kullanıcının push/merge/deploy yapmama talebi korunur.

AOS'un güncel handoff'u bağımsız özgün bütçe için authenticated transport ister.
Mevcut `read_original_budget` trusted host API'sidir; AOS'un Scientist özel DB'sini
okuması veya terminal içinden bütçe kopyalaması çözüm değildir. Sonraki dar
entegrasyon: aynı mevcut socket/target capability/reconcile kotası üzerinde,
terminal evidence ve özgün budget witness'ı **aynı transaction** içinde export
eden açık opt-in transport sürümü. Mevcut evidence.v2 ve terminal-v1 bytes/saatler
korunmalı; yeni tam schema/pin karşılıklı review edilmeli. İki public read metodunu
ardışık çağırmak tutarlı snapshot garantisi sağlamaz.

AOS tarafında current target-rights/resolver provider, fiziksel cleanup proof,
result validator ve durable inference resolution bağlanmalıdır. ACK, idle veya
journal pending-count=0 GPU release kanıtı değildir. Bunun ardından tek Scientist
koordinatörü, mevcut izinler ve gerçek kaynak rezervasyonu altında sınırlı modeli
çalıştırabilir. Bu teslim GPU kabulü, araştırma veya eğitim sonucu değildir.

## AOS oturumuna kısa aktarım

Scientist client39/journal44 üyeliklerini tanıyor; reviewed_snapshot ile yukarıdaki
exact HEAD/diff/selected/untracked pinleri kaynak bakımından geçti. Mevcut ortak
evidence transport SHA `7e76687f7f0e3e4f8f5dba4d0edbc4d70dbba192f567f92d373b80b056fb12b7`
değişmedi. Bütçe aktarımı henüz mevcut wire içinde yoktur. Yeni opt-in wrapper ve
atomic retained-budget/evidence export için karşılıklı schema review gerekir;
mevcut client/journal yetki zinciri korunacaktır. Runtime aktivasyonu ve fiziksel
GPU kabulü ayrıca bekler; bu oturum tek GPU test yürütücüsüdür.

## Kabul kapsamı

Geçen: kaynak snapshot uyumu, 44 üyelik, drift ve Git driver retleri.
Kalan: authenticated budget aktarımı, AOS proof/provider/resolution composition,
exact runtime/config/principal ve tek scheduler rezervasyonu.
Çalıştırılmayan: gerçek AOS→Scientist GPU devri, kontrollü GPU iptal/toparlanma,
model/context/quantization kapasitesi, VRAM tepe ve devir gecikmeleri.
Özgün kabul toplamı değişmedi: **11 passed / 7 partial / 4 open**.
Lisans ve genel CI işleri bu entegrasyonun dışında izlenir.
