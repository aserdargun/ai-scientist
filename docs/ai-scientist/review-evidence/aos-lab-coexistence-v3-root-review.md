# AOS/Lab V3 sürücüsü: uygulama öncesi inceleme

2026-09-25. İncelenen sabit sürücü SHA-256:
`1f6dd10c1c1f652e4b9f3a70d4aa9aea4e444c4c2e0e5806aa0a18043c03629b`.
V3 dosyaları değiştirilmedi. Sürücü, test ve beş çıktı dosyasının kayıtlı
hash'leri yeniden doğrulandı (tool chunk `56f681`, gerçek exit 0, 7/7).
Bu dosya kontrolü servis veya GPU çalıştırmadı.

## V4 öncesi kapatılması gereken bulgular

1. **Temizlik hatası başarılı çıkış verebilir.** `execute()` içindeki
   `finally`, sahipli süreç boşaltılamadığında `self.outcome` değerini
   değiştiriyor. `main()` dönen sonuca yine `status=passed` ekleyip exit 0
   veriyor. Nihai kabul, temizlik sonrası durumu da doğrulamalı.
2. **Başlangıç hatasında sahiplik kaydı geç oluşuyor.** AOS principal
   kimliği token/API hazır olduktan sonra kaydediliyor. Önce hata olursa
   temizlik kendi başlattığı servisin neslini bilmiyor. Servis nesli
   başlatma sonrasında hemen bağlanmalı; uygulama hazır olma kontrolü
   ayrıca yapılmalı. Lab dispatch yolu için de aynı sınır incelenmeli.
3. **Birlikte ilerleme kanıtı geçmiş ticket'ları sayabilir.** Örneklerdeki
   ticket listesi kümülatif. Birlikte çalışma aralığından önce tamamlanmış
   `done` ticket, aralık içinde yeniden gözlendiği için sayılabiliyor.
   Tamamlanma/geçiş anı aynı boot, run ve süreç nesliyle gerçek aralığa
   bağlanmalı; yalnız gözlem anının aralıkta olması yeterli değil.
4. **Öneri üretim aşaması eksik gözleniyor.** `proposal_active` yalnız
   deney tablosundan çıkarılıyor. Yerel model çağrısı, öneri deney satırı
   yazılmadan önce gerçekleşiyor. Baseline tamamlanması ve hash'i
   doğrulanan provider-attempt checkpoint'leri de araştırma etkinliği
   hesabına katılmalı. AOS görevi bu gerçek çağrı aralığında başlatılabilmeli.
5. **Başlatma kanıtının kapsamı sınırlı.** V3 araştırmayı HUMAN sahipliğiyle
   typed `/api/lab/start` üzerinden başlatıyor. Bu, M0.AOS.5'in gerçek model
   kararıyla araştırma başlatma şartını kanıtlamaz. Gerçek model başlatma
   akışı ayrı doğrulanmalı veya sınırlama açıkça korunmalı.

V3 CPU testleri ve girdi hazırlığı geçerli tarihsel kanıttır; sürücü gerçek
M0.AOS.5/7 kabulü için hazır sayılmıyor. Düzeltmeler ayrı V4 dosyalarında
hazırlanacak. Gerçek GPU denemesi, canlı AOS geliştirmesiyle bekleyen
koordinasyon yanıtına bağlı; yabancı süreçler durdurulmayacak.

## Özel bytecode kopyasının kapsamı

`aos-lab-coexistence-v3-bytecode-scope-note.md`, yalnız izole Decider
kopyasındaki dört `.pyc` dosyasının karantinaya alınmasını açıklar.
İlgili JSON alanı `pinned_inputs.unlisted_bytecode.source_cache_removed`.
Canlı `/home/cachyos/aos` kaynakları ve cache'i bu işlemde değiştirilmedi.
