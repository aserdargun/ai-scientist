# AOS için bağımsız özgün bütçe okuması

2026-10-01. Scientist tabanı `91c2109b1c6af10dce4f41894050324c97c6e866`.
Bu kaynak teslimi [evidence socket sözleşmesini](87-aos-evidence-socket-integration.md)
değiştirmez; yeni socket, GPU otoritesi veya scheduler tablosu oluşturmaz.

## Kapanan eksik

AOS `ScientistTerminalVerifier(expected_budget=...)`, kontrol ettiği terminal
raporundan alınmamış özgün bütçe ister. Allocation + config/history tek başına
yeterli değildir: ilk intent `created_boottime`, ilk submit `queue_deadline`
ve readiness'te ilk atanan `inference_deadline` bu kaynaklarda tam bulunmaz.
History capture zamanı veya lease total deadline'ından bunlar tahmin edilemez.

`ControlStore.read_original_budget` güvenilir host adaptörüdür. Tek tutarlı
read transaction'ında özgün intent/budget/allocation/readiness sütunlarını
okur; **receipt_json hiçbir aşamada bütçe kaynağı değildir**. Terminal durum
zorunludur, böylece gelecekteki readiness assignment'ı erkenden dondurulmaz.
Original owner/target, typed retained reconcile authority, reserved original
budget hash ve allocation/request/principal/admission/deadline bağları
doğrulanır. Son authority doğrulaması commit öncesindedir.

Çıktı `aos-scientist-original-budget-witness.v1`, integer version1:
target, profile/deployment/config/content pinleri, özgün admission veya cleanup
authorization SHA, allocation SHA, `budget_canonical` ve `budget_sha256`.
Özgün float saatleri aynı kalır; okuma quota, ID, deadline veya clock'u
değiştirmez. Snapshot doğrulanmış mevcut retained sütunlardan gelir; yeni
authority değildir. Çağıran trusted composition witness'ı ayrı, immutable
olarak saklamalıdır. Bu API public socket operation olarak sunulmaz.

## İcra ve sınır

27 birleşik CPU kontrolü / exit0 / kaynaklar değişmeden tamamlandı; parent
3.809978355 saniye. Completed/canceled bütçe kaydı, readiness override,
never-admitted null phase deadline'ları, inflight ret, yanlış target ve geç
authority revoke denetlendi. Terminal bütçesi değiştirilince witness aynı
kaldı; daha geç saatle tekrar okuma yeni budget üretmedi. Snapshot alanları
sentetik fixture'larda test edildi; gerçek AOS/GPU kabulü değildir.

Zorunlu kalite kapısı **1723 passed / 7 opt-in skipped / 120 GPU-live
deselected**; yedi komut gerçek exit0. Parent 121.569865084 saniye; tüm Python
kaynakları koşu boyunca ve teslim kontrolünde aynı hash'te. Kaynak hash'leri ve
komut kanıtı [makine kaydındadır](review-evidence/aos-original-budget-source-delivery.json).
Allocated/no-child cleanup, null-budget tombstone ve caller restart bu
adaptörle tamamlanmış sayılmaz. Budget witness fiziksel drain kanıtı değildir.

## AOS oturumuna aktarım

- Mevcut evidence transport canonical SHA
  `7e76687f7f0e3e4f8f5dba4d0edbc4d70dbba192f567f92d373b80b056fb12b7`
  aynen korunur. Yeni wire alanı veya pin yükseltme yoktur.
- Koordineli kabulün trusted host composition'ı özgün Store retained authority
  ile bu snapshot'ı okuyabilir. AOS, current source/resolver/target bağlarını
  doğruladığı trusted kanal üzerinden witness'ı ayrı saklayıp
  `json.loads(budget_canonical)` değerini açık `expected_budget` olarak verir.
  Terminal bütçesinden doldurma veya kaynak bağı olmayan sidecar kabul edilmez.
- Resolver/proof/result callback'lerini ve durable journal resolution'ı bağlamak
  AOS tarafında hâlâ gereklidir. Bu teslim üretim courier kanalını kurmaz veya
  AOS kayıtlarına yazmaz. Karşılıklı review edilmiş exact source/config ve rezervasyon
  doğrulanmadan Scientist gerçek GPU kabulünü başlatmaz.

Host yalnız salt okunur gözlendi: kullanıcı oturumunda API, console, tünel ve
Director drain servisleri çalışıyor; nvidia-smi compute listesinde masaüstü
KWin süreci görüldü. Bu anlık liste kullanıcı işi yokluğunun veya VRAM tepesinin
kanıtı değildir. Servis/süreç/AOS dosyası değiştirilmedi. Model/quantization/
context, VRAM tepe ve GPU devir gecikmesi ölçülmedi. Kabul sayıları değişmedi;
lisans/genel CI ve gerçek araştırma/eğitim işleri ayrı kalır.

## Kaynak kapısının düzeltilmesi gereken koşulu

Temiz/commit edilmiş AOS worktree şartı kullanıcının veya mimarinin şartı
değil, mevcut preflight'ın daha sıkı tercihidir. Mimari review ayrı opt-in
oturumda diff/hash takibini destekler. Sonraki açık `reviewed_snapshot` modu
HEAD, tracked diff ve untracked dosyaları da kapsayan seçili manifesti ayrı
kimlikler olarak doğrulamalıdır; çift okumada drift reddedilmeli, public HEAD
ile yerel snapshot karıştırılmamalıdır. Bu mod henüz uygulanmadı. Mevcut
dirty/untracked ret sonucu runtime izni olarak çevrilmez. Runtime kaynak/
bağımlılık/config closure, current principal, budget/proof callbacks ve GPU
rezervasyonu ayrıca zorunludur. AOS'un yeni client kaynakları da runner'ın
kullanacağı gerçek kaynak profiline dahil edilmelidir.
