# Seçilen deney bulgularını yeni çalışmaya taşıma

Tarih: 2026-10-03. Ürün önizlemesi: **0.44.0**, ayrı yerel
`feat/omr-stream-v1` çalışma ağacı. Native ana dalın AOS kabulü ayrıdır.

## Arayüzden deneyin

**aserdargun:** http://HOST:8788/ · **CachyOS:** http://127.0.0.1:8789/.
Sayfayı yenileyin, **Eylemci → Doğrulanmış deney hafızası** bölümünü açın.

1. `89dc9a5c-007d-480e-8594-6e14c035a0b9` koşusunu seçin.
2. LSH, OPTICS ve SOM kayıtlarından uygun olanları işaretleyin; tek kaynak
   koşudan en fazla sekiz kayıt seçilebilir.
3. **Seçilenlerle deney tasarla** düğmesini kullanın. Veri, parametre ve
   bütçeyi belirleyip CPU parametre karşılaştırmasını başlatın.
4. Terminal koşuyu hafızada açın. **Seçili geçmiş bulgular Director
   bağlamına taşındı** alanı, kaydın gerçekten öneri bağlamına bağlandığını
   doğrular. Yalnız kabul edilmiş fakat öneri üretmemiş koşu ayrıca gösterilir.

Hazır tamamlanmış örnek: **`3873c13b-66fb-475c-916e-d2f28633bc2c`**.
Bu profilde yerel model çağrıları kapalıdır. CPU karşılaştırması seçilmiş
parametreleri izler; geçmiş bulgular parametre sırasını kendiliğinden değiştirmez.

## Gerçek CPU sonucu

| Ölçüm | Sonuç |
|---|---|
| Veri | Yeni sentetik step snapshot, seed 4103; 192 eğitim / 96 değerlendirme satırı |
| Geçmiş | Önceki LSH/OPTICS/SOM çalışmasından üç doğrulanmış dev bulgusu |
| Yeni ölçümler | 9 baseline + 1 bağımsız LSH aday ölçümü |
| Durum / süre | completed / admission başlangıcından terminal okumaya 222,87 saniye |
| Bütçe | 600 saniye; model çağrısı/token/GPU işi sıfır |
| Aday kararı | DISCARD; ölçülmüş iyileşme yok |
| Bağlantı | 3 geçmiş kayıt → 1 öneri bağlamı; snapshot hash'i doğrulandı |
| Tekrar / eski kayıtlar | Aynı idempotency anahtarı aynı koşuyu döndürdü; eski iki rapor değişmedi |
| Kapanış | Director süreci/cgroup, 10 Scorer işinin unit/cgroup'ları ve sandbox konteynerleri temiz; kuyruk boş |

Rapor SHA-256:
`11bc05689ffb9d5798a3ae155aef543dd4748b47c132129cc76553f46cc67598`.
Geçmiş bağlam SHA-256:
`595f16dbbbe38b3112527f0e77ac8ca9c674db845bfdce749a3a4bd2b07d1111`.

[Seçilmiş teslim kanıtı](review-evidence/field-lab-prior-findings-context-20261003.json)
commit çiftlerini, kaynak/imaj hash'lerini, süreyi, browser ve cleanup
kanıtlarını içerir. VRAM ve GPU devir süreleri bu CPU koşusunda ölçülmedi.
Gerçek saha araştırması, model eğitimi veya AOS GPU kabulü yapılmadı.

## Bağlam ve eğitim sınırı

İstemci yalnız kaynak koşu/rapor ve deney/trajectory hash referanslarını
seçer. Sunucu aynı sahibin terminal kaydını, dev kapsamını, provenance ve
ölçüm kontrollerini doğrular. `prior-dev-findings.v1` snapshot'ı en fazla
16 KiB'dır; değişmez isteğe, Director bağlamına ve trajectory hash zincirine
bağlanır. Retry/resume geçmiş kaynağı yeniden taramaz. Eski ölçümler yeni
kalibrasyona veya KEEP kararına yazılmaz; yeni Scorer bağımsız çalışır.

Geçmiş skorlar danışma verisidir. Holdout, altyapı hatası veya geçersiz hash
uygun bulgu sayılmaz. Farklı tarihsel harness sürümleri kaynak kimliğiyle
saklanır; eski ölçüm güncel ölçüm diye sunulmaz.

SFT ihracı, kaynak geçmişin eğitim izin zinciri incelenmemişse
`prior_findings_permission_not_reviewed` ile kaydı dışlar. Bu kontrol
özgün gözlenen prompt üzerinde yapılır; metni temizlemek izni sağlamaz.
Kural sürümü `local-sft-reviewed.v2`dir. Öğretmen taslağı gerçek eğitim,
öğrenilmiş adapter veya skill yayını değildir.

## Doğrulama ve kalan işler

Son kaynakta yedi kalite komutu exit 0: **3305 passed, 7 skipped,
177 GPU/live deselected**. Önceki başarısız denemeler korunmuştur; kod tipi/
test düzeneği düzeltmelerinden sonra disk rezervi ve Unix socket yol boyu
engelleri ayrı geçici test diziniyle çözüldü. Son imajda 208 dosya birebir
eşleşti. Gerçek UI okumasında masaüstü/mobil, kaynak seçilebilirliği ve
bağlam referansları doğrulandı; POST veya sahte yanıt kullanılmadı.

## AOS ile hazırlık uyumu

Scientist MAIN çağırıcısı, AOS'un ayrı `scientist-shared-v1` çalışma alanının
kimliğini ve başlangıç girdilerini doğrulayacak şekilde güncellendi. Güncel
156 AOS kaynak dosyası eşleşti; gerçek template/plan/provision ve boş
workspace kimliği bağımsız okundu. AOS 3.14 ile 59 odaklı CPU kontrolü geçti.
AOS kurulu olmayan Scientist 3.12 ortamında 17 kontrol geçti, 42 harici
entegrasyon kontrolü açık opt-in olarak atlandı. Önceki 117/v3 sonucu
tarihsel kanıttır; güncel v4 sonucu ayrı kaydedildi.

Bu çalışma alanında servis veya GPU işi başlatılmadı. Native GPU işlerinin
ortak admission/durdurma mekanizmasına alınması, taze çalışma yetkisi,
gerçek caller/broker kimliği ve doğrulanmış cleanup bağlantısı tamamlanmadan
ortak GPU kabulü yapılamaz. [AOS aktarım notu](06-aos-coordination.md).

Kalanlar: yerel modelin bu bulgularla yaptığı araştırmanın faydasını bağımsız
ölçmek; otomatik bilgi/skill seçimi; izinli öğretmen veri paketi, sınırlı
öğretmen/adapter eğitimi ve bağımsız terfi/geri dönüş. AOS için kaynak/config
pinlerine bağlı taze yetki, native admission dışlaması ve tek koordineli gerçek GPU devir/
iptal kabulü açıktır. Lisans, genel CI ve taşınabilir temiz kurulum ayrı işlerdir.
