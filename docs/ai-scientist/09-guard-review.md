# Guard uygulaması: bağımsız inceleme

Tarih: 2026-09-24. Önceki dilim `8341b30` ile kalıcı puanlama ve plan kapanışı tamamlandı; tam Director döngüsü ve aşağıdaki guard kabulleri hâlâ açık.

## Aday test kataloğu

[Kaynak üreticisi](review-evidence/guard_review_candidates.py), `build_candidate() -> ADPipeline` sözleşmesinde 19 bağımsız aday içerir. [Katalog](review-evidence/guard-review-candidate-catalog.json), her adayın byte hash'ini, beklenen ret türünü ve kapsamını kaydeder. [Gerçek yürütme kanıtında](review-evidence/guard-docker-review.json) 19/19 senaryo beklenen sonucu verdi: dört hardcoding adayı çalıştırılmadan reddedildi; kalan 15 aday, toplam 106 gerçek Docker fazında sınandı. Aday kodu ve pickle host'ta açılmadı.

- Pozitif kontroller: causal robust normalizasyon, küçük ölçekli fakat sabit olmayan skor, her score için aynı fit kopyasından başlatılan stateful aday ve `ctx.seed` kullanan rastgele fit.
- Negatif kontroller: yalnız fit sırasında tohumsuz RNG, score sırasında RNG, eval istatistiğiyle normalizasyon, centered rolling, sabit/NaN/inf/eksik uzunluk, NRM zaman rampası, görev kimliği, UTC ile eşdeğer eval timestamp literal'i, 64 elemanlı float listesi ve süreyi aşan fit.
- Determinizm denemesi aynı seed ile **fit ve score'u birlikte** tekrarlar. Aynı fit dosyasını iki kez score etmek fit sırasındaki rastgeleliği yakalamaz.
- Nedensellik denemesi aynı donmuş fit artefaktından her çağrı için ayrı score konteyneri başlatır. Pickle veya aday modülü host/Scorer sürecinde açılmaz.
- Position bias, NRM görevlerinin uygun kümesinde değerlendirilir; EVT/PDM çıktısına sırf monoton diye aynı kural uygulanmaz. Statik hardcoding taraması ile çalışma zamanı ağ/dosya gözlemi farklı kontrollerdir.

## Gerçek yürütme sonuçları

[İnceleme script'i](review-evidence/review_guard_docker.py), üretim `run_guarded_seed_evaluation` yolunu ve dönen gerçek skorlar üzerinde `check_complete_suite_position_bias` kontrolünü çağırır. Kaynak, harness, imaj ve aday hash'leri; her fazın konteyner kimliği, girdisi ve fit artefaktı hash'i kaydedilir. Koşu boyunca kaynak/harness değişmedi; nedensellik karşılaştırmalarındaki dört score fazı aynı donmuş fit artefaktını ayrı konteynerlerde açtı.

| Senaryo | Gözlenen sonuç |
| --- | --- |
| Temiz causal, küçük ölçekli causal, stateful yeniden açılış, seed'li fit | 4/4 geçti |
| Yalnız fit veya score sırasında tohumsuz RNG | Determinizm reddi |
| Eval istatistiği, bunun `1e-12` ölçekli hali, centered rolling | Nedensellik reddi |
| Sabit, NaN, infinity, yanlış uzunluk | İlgili sabit/nonfinite/uzunluk ret kodu |
| Tek NRM görevi üzerinde zaman rampası | Position bias reddi |
| Görev kimliği, UTC aralığı, 64 pozitif/negatif sayı literal'i | Statik hardcoding reddi |
| İki saniyelik toplam bütçede beş saniye uyuyan fit | Timeout reddi |

İnceleme host süreci transient user service içinde 1 GiB RAM, sıfır swap, 1 CPU ve 64 task ile sınırlıydı. Her aday fazı 512 MiB RAM, 1 CPU ve 64 PID ile P=1 kapasitesini kullandı. Başarılı konteynerler kaldırıldı, özel çalışma dizini boşaltılıp silindi; [başlatma ve çıkış kaydı](review-evidence/guard-docker-launch-review.json) host inceleme servisinin exit 0 ile inactive/not-found durumuna döndüğünü de doğrular. Canlı AOS üzerinde işlem yapılmadı; GPU ve public veri kullanılmadı.

Bu kanıt per-seed ve tek görevli sentetik guard davranışını doğrular. Tam süitin beklenen NRM görevlerinin kimlik/kapsam doğrulaması, dönüşümlü iki nedensellik görevi, Director/Referee KEEP kapısı, yasak erişim audit'i ve gerçek public veri kabulü ayrıca tamamlanmalıdır. Bir görevin guard'larının geçmesi tam süit KEEP uygunluğu anlamına gelmez.

## Rootless seccomp notification kapasitesi

[Gerçek Docker kontrolü](review-evidence/seccomp-notify-capability-review.json), mevcut sabit sandbox imajında UID 10001, `cap-drop ALL`, `no-new-privileges`, ağsız profil, 128 MiB RAM ve 0.25 CPU ile çalıştı. Host ayarı veya çalışan AOS servisi değiştirilmedi.

[İnceleme script'i](review-evidence/review_seccomp_notify_capability.py) yalnız zararsız raw `getpid` sistem çağrısını hedefler. Alt süreç BPF notification filtresi kurdu, listener FD'yi Unix socket üzerinden parent'a verdi ve kendi kopyasını kapattı. Çekirdek bildirimi ayrı parent sürecine ulaştı; parent EPERM döndürünce çağrı `-1/EPERM` ile reddedildi. Bildirim PID'si doğru çocukla eşleşti; süreç exit 0, toplam yaklaşık 0.38 saniye. Ek yetki veya root UID gerekmedi.

Bu sonuç yalnız mekanizmanın mevcut ortamda kullanılabildiğini gösterir. Üretimde güvenilir erişim kaydı, listener/parent bütünlüğü, bounded event yükü, çocuk/thread/ABI kapsamı ve izleyici kaybında güvenli ret ayrıca uygulanıp sınanmalıdır. Mevcut kernel namespace/capability/cihaz/ağ sınırları esas izolasyon olarak kalır.

[Linux man-pages açıklaması](https://www.man7.org/linux/man-pages/man2/seccomp_unotify.2.html), user notification mekanizmasının güvenlik politikasının yerini alamayacağını açıklar. `CONTINUE` ile işaretçi içeriğine göre izin vermek kontrol/kullanım yarışına açıktır; hedefin daha öncelikli seccomp filtresi kurabilmesi de bildirimi atlatabilir. Buradaki prototip `CONTINUE` kullanmaz ve değişebilir dosya yolu argümanına izin kararı vermez. Olası üretim gözlemcisi, zaten yasak olan çağrıları kaydetme/reddetme yönünde incelenmelidir.

[CPython 3.12 açıklaması](https://docs.python.org/3.12/library/sys.html#sys.addaudithook), Python audit hook'larının kötü niyetli kod tarafından atlatılabileceğini belirtir. Adayın kendi yazdığı “ihlal yok” kaydı veya Python hook'u, bağımsız erişim kanıtı sayılmaz.

## WIP evaluator üzerinde yeniden üretilen açıklar

[Negatif kontrol](review-evidence/guard-preflight-before.json), henüz commit edilmemiş evaluator kaynak hash'iyle dört sorunu doğruladı. [Script](review-evidence/review_guard_preflight.py) aday kodu çalıştırmaz; yalnız küçük sentetik veriyi ve AST/nümerik yardımcıları kullanır. Bunlar başarılı kabul kontrolleri değildir.

1. Üç satırlı sensör DataFrame'inin `attrs` alanına eklenen sahte `eval_labels` ve canary, Arrow pandas metadata'sında ve tekrar açılan DataFrame'de aynen kalıyor. Sütun kara listesi metadata sızıntısını engellemiyor.
2. Küçük ölçekli skorların iki katına çıkması, `max(1, magnitude)` normalizasyonu nedeniyle `2e-12` fark diye ölçülüp `1e-7` göreli sınırından geçiyor. Ölçekleme leaky adayın sıralama metriğini değiştirmeden guard'ı atlatabilmesine yol açabilir.
3. 64 negatif float literal'i AST'de unary düğümler olduğu için mevcut sayısal liste taramasından geçiyor.
4. Eval zaman aralığının içinde bulunan fakat verilen uç noktalarla eşit olmayan timestamp literal'i geçiyor; gereksinim yalnız tam timestamp eşliği değildir.

[Düzeltme kontrolü](review-evidence/guard-preflight-fixed-review.json) 8/8 kontrolü geçti. Ek incelemede, sayıya çevrilebilen kategorik sütunun kullanılmayan özel metin kategorisini de Arrow dictionary içinde taşıdığı görüldü. Aktarım artık doğrulanmış float64 değerlerinden ve onaylı sütun adlarından yeniden oluşturulur; kullanılmayan kategori canary'si payload'a geçmez. Kaynak DataFrame değişmeden kalır. Küçük ölçekli sızıntılı aday ve negatif literal listesi, gerçek yürütme kataloğunda da reddedildi.

Kamu veri yolunda NaN girdinin tamamını yasaklamak çözüm değildir: sürümlü materyalizasyon üç adımdan uzun boşluğu NaN/gap maskesiyle korumalı; skorların sonlu olması şartı ve girdideki eksik değer sözleşmesi ayrı tutulmalıdır. Bu dilim sonlu sayısal girdilerle sınırlıdır; gerçek kamu veri materyalizasyonu kabulü açık kalır.
