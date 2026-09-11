from service.drama_subtitle_language import classify_subtitle_text


def test_portuguese_is_not_misclassified_as_english():
    result = classify_subtitle_text(
        "Minha alma gêmea e meu marido, mas ninguém sabe que eu existo. "
        "Você está carregando um filho. Agora não tem ninguém aqui para ajudar."
    )

    assert result.language_code == "pt"
    assert result.confidence >= 0.72


def test_english_latin_text_remains_english():
    result = classify_subtitle_text(
        "My husband left the house this morning, and nobody knows where he went. "
        "Please call the hospital and ask whether he is there."
    )

    assert result.language_code == "en"
