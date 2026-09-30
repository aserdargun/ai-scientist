# Thompson politikasının Director entegrasyonu — 2026-09-27

Ana runtime **0.31.0**. Uygulama GPT-6 Luna / high, kaynak incelemesi
GPT-6 Astra / high tarafından yapıldı. Bu dilim deney seçimini kalıcı
Director durumuna bağlar; yeni süreçle araştırmaya devam etme kabulü değildir.

## Uygulananlar

- Run kimliğinden türetilen seed, canonical strateji JSON'u ve hash'i
  `director-loop-state.v2` içinde saklanır. Seçim, model çağrısından önce
  yazılır ve geri okunarak doğrulanır; model seçilen hareketi değiştiremez.
- Terminal deney sonucu posterior ve sayacı bir kez ilerletir. Parse hatası
  abandon olarak çözülür; altyapı hatasında bekleyen seçim ve tüketilmiş bütçe
  korunur. Mevcut dispatcher hata davranışı sürer; desteklenen yeni bir
  resume komutu eklenmedi.
- Holdout geri alma işlemi strateji geçmişini korur. Eski bütçe checkpoint'i
  daha yeni döngü durumunu geri saramaz; aynı failure checkpoint'i tekrar
  okunurken yeni elapsed süre ile çelişen bir kayıt üretilmez.
- Raporlar tarihi v1 ve güncel v2 durumlarını okuyabilir. Aktif v1 durumu,
  önceden kaydedilmiş strateji geçmişi bulunmadığından devam ettirilmez.

## İcra edilen doğrulamalar

| Kontrol | Gerçek sonuç |
|---|---|
| Hedefli strateji/döngü/holdout testleri | Session 32262, exit 0; **88 passed**, 3,680 sn; sürücü tepe belleği 165,1 MiB |
| Sandbox build ve byte eşliği | Session 29048, exit 0; **103 runtime dosyası**, 105 snapshot kaynağı eş; 5,930 sn |
| Tam kalite kapısı | Session 9344, exit 0; yedi komutun tamamı 0; **521 passed / 7 skipped / 22 deselected**; strict mypy 94 kaynak; Pylint 9,37 |
| Kalite süresi/kaynakları | 56,052 sn duvar, 51,310 sn CPU; 493,3 MiB tepe, swap 0; 3 GiB/2 CPU/128 görev üst sınırı |

Yeni imaj:
`sha256:aad6bf2efcfdda040a277db0ca46e5b5449f4547007861a1e5f7128509dd683f`.
Harness hash:
`d65fc451be56eba6c141f40e0ee18282f05b26189bd697f0b39a33637bbcb553`.
177 kaynak dosyasının hash'i kalite kapısı boyunca değişmedi.
İmaj kontrol konteyneri ağsız, salt okunur, 512 MiB/1 CPU/64 PID sınırındaydı;
sürücü bellek ölçümü Docker kardeş süreçlerini kapsamaz.

Astra'nın istediği iki ek regresyon ana testte bulunur: yalnız eski veya
aynı sıra numaralı receipt varken bütçe değişmez; uygulanmış terminal durum
JSON'dan yeniden yüklendikten sonra ikinci recovery hiçbir checkpoint,
posterior/RNG/sayaç veya bütçe değişikliği yapmaz.

Kaynak incelemesi: [Astra kaydı](review-evidence/thompson-loop-030-source-review.json).
Tam komut çıktıları ve hash bağı:
[kalite kaydı](review-evidence/thompson-integration-031-quality-gate-binding.json),
[imaj kaydı](review-evidence/thompson-integration-031-image.json).

## Açık kalanlar

Güncel v2 için gerçek PostgreSQL senaryosu, bütün Director süreç restart'ı,
EXPLORE aile doğrulaması, gerçek public veri/model araştırması, eğitim ve
AOS birlikte çalışma kabulü açık. Araştırma devamının sınırları
[35-director-restart-scope-review.md](35-director-restart-scope-review.md)
içindedir. Kalibrasyon 0022/0023 ve baseline CLI ayrı çalışma ağaçlarındadır;
bu gate onların ürün kodunu kapsamaz. Kabul sayıları **10 geçti / 8 kısmi /
4 açık** olarak kalır. PR açılmadı ve merge yapılmadı.
